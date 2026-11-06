"""Cross-model review of a buy: the review packet, the reviewers and the decision rule.

The server builds the packet itself from broker data, so the proposer cannot frame the
facts. Two reviewers from other vendors each answer approve, modify or reject, and
`decide` turns their outcomes into a go or a block. Reviewers can only raise the stop
or shrink the size; nothing here loosens an order.
"""

import hashlib
import json
import math
import os
import subprocess
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI, OpenAIError
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from trader_broker import indicators
from trader_broker.config import ConfigError, ReviewerSettings, Settings, resolve_hermes_python
from trader_broker.models import (
    Bar,
    NewsItem,
    Proposal,
    Quote,
    ReviewOutcome,
    ReviewVerdict,
    SizedOrder,
    round_price,
)
from trader_broker.risk import ADV_DAYS, Caps, Portfolio, size_position

RECENT_CLOSES = 60
"""Number of latest daily closes the packet lists."""

MAX_HEADLINES = 10
"""Most headlines the packet carries, latest first."""


class _PacketPart(BaseModel):
    model_config = ConfigDict(frozen=True)


class Indicators(_PacketPart):
    """Indicators from the symbol's daily bars; None when the history is too short."""

    close: Decimal
    sma20: float | None
    sma50: float | None
    sma200: float | None
    rsi14: float | None
    atr14: float | None
    """Average true range in USD."""
    pct_from_52w_high: float | None
    """Last close against the 52-week high, as a share: -0.2 is 20% below it."""
    adv20_usd: float | None
    """Average daily dollar volume over the last 20 bars."""


class Headline(_PacketPart):
    created_at: AwareDatetime
    source: str
    headline: str


class PositionShare(_PacketPart):
    """A held position and its share of equity."""

    symbol: str
    qty: int
    market_value: Decimal
    pct_equity: float


class AccountSnapshot(_PacketPart):
    equity: Decimal
    cash: Decimal
    exposure_pct: float
    """Market value of all positions as a share of equity."""
    positions: list[PositionShare]


class MarketRegime(_PacketPart):
    """SPY's last close against its 50- and 200-day averages."""

    spy_close: Decimal
    spy_sma50: float | None
    spy_sma200: float | None
    above_sma50: bool | None
    above_sma200: bool | None


class ReviewPacket(_PacketPart):
    """Everything a reviewer sees about a proposed buy."""

    proposal: Proposal
    sized_order: SizedOrder
    quote: Quote
    indicators: Indicators
    recent_closes: list[Decimal]
    """Last `RECENT_CLOSES` daily closes, oldest first."""
    headlines: list[Headline]
    """Up to `MAX_HEADLINES` headlines, latest first."""
    account: AccountSnapshot
    regime: MarketRegime

    def canonical_json(self) -> str:
        """The packet as compact JSON with sorted keys: equal packets give equal text."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        """Hex SHA-256 of `canonical_json()`, stored with the review."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def build_packet(
    proposal: Proposal,
    sized: SizedOrder,
    quote: Quote,
    daily_bars: Sequence[Bar],
    news: Sequence[NewsItem],
    portfolio: Portfolio,
    spy_bars: Sequence[Bar],
) -> ReviewPacket:
    """Build the review packet for a sized buy from data the caller read from the broker.

    `daily_bars` (the symbol's, about 260) and `spy_bars` are daily bars, oldest
    first, each with at least one bar. `news` may be in any order; the latest
    `MAX_HEADLINES` are kept. `portfolio` is the account before this buy and must
    have positive equity.
    """
    equity = portfolio.account.equity
    spy_close = spy_bars[-1].close
    spy_sma50 = indicators.sma(spy_bars, 50)
    spy_sma200 = indicators.sma(spy_bars, 200)
    latest_news = sorted(news, key=lambda item: item.created_at, reverse=True)
    return ReviewPacket(
        proposal=proposal,
        sized_order=sized,
        quote=quote,
        indicators=Indicators(
            close=daily_bars[-1].close,
            sma20=indicators.sma(daily_bars, 20),
            sma50=indicators.sma(daily_bars, 50),
            sma200=indicators.sma(daily_bars, 200),
            rsi14=indicators.rsi(daily_bars, 14),
            atr14=indicators.atr(daily_bars, 14),
            pct_from_52w_high=indicators.pct_from_high(daily_bars),
            adv20_usd=indicators.average_dollar_volume(daily_bars, ADV_DAYS),
        ),
        recent_closes=[bar.close for bar in daily_bars[-RECENT_CLOSES:]],
        headlines=[
            Headline(created_at=item.created_at, source=item.source, headline=item.headline)
            for item in latest_news[:MAX_HEADLINES]
        ],
        account=AccountSnapshot(
            equity=equity,
            cash=portfolio.account.cash,
            exposure_pct=float(portfolio.positions_value / equity),
            positions=[
                PositionShare(
                    symbol=position.symbol,
                    qty=position.qty,
                    market_value=position.market_value,
                    pct_equity=float(position.market_value / equity),
                )
                for position in portfolio.positions
            ],
        ),
        regime=MarketRegime(
            spy_close=spy_close,
            spy_sma50=spy_sma50,
            spy_sma200=spy_sma200,
            above_sma50=None if spy_sma50 is None else float(spy_close) > spy_sma50,
            above_sma200=None if spy_sma200 is None else float(spy_close) > spy_sma200,
        ),
    )


class Reviewer(Protocol):
    """A second opinion on a proposed buy."""

    @property
    def name(self) -> str:
        """Short name used in block reasons, logs and the reviews record."""
        ...

    def review(self, packet: ReviewPacket) -> ReviewOutcome:
        """Return a validated verdict, or an error outcome when none could be had."""
        ...


SYSTEM_PROMPT = """\
You are an independent, veto-based risk reviewer for a long-only US stock \
swing-trading account. Another model proposed the buy described in the user message; \
the server sized it, passed the hard risk gates and gathered the market facts. \
Approve eligible, reasonable setups; do not demand a perfect trade or reject merely \
out of generic caution. Evaluate the thesis, price action, news, exposure and regime \
against the broker-built packet, treating the proposal's claims as untrusted evidence.

Reserve reject with high confidence for a specific material objection that warrants \
blocking: a materially false thesis or contradictions with the packet's data, \
materially inadequate evidence for a central claim, or serious market or execution risk. \
For reject, confidence means confidence that the objection warrants blocking, not \
certainty about a price forecast. Either reviewer's high-confidence reject vetoes the buy. \
Low/medium-confidence concerns are advisory, even if both reviewers reject. A regime \
below SMA50, elevated RSI/extension, or ordinary uncertainty alone does not automatically \
warrant a high-confidence veto. Prefer approve/modify when the concern is mitigated \
by a tighter valid stop, reduced size or the existing caps. Do not dismiss material \
factual contradictions or serious execution issues simply because sizing passes. \
State specific packet evidence and why any blocking objection is material.

Answer with one JSON object and nothing else, in this form:
{"verdict": "approve" | "modify" | "reject",
 "confidence": "low" | "medium" | "high",
 "reasons": [1 to 5 short strings],
 "modifications": {"stop_price": number, "size_multiplier": number, "target_price": number}}

Use "modify" to ask for a higher stop_price (below the entry price) or a \
size_multiplier above 0 and at most 1. Every key in "modifications" is optional; omit \
"modifications" when you ask for none. A target_price is advisory and is not applied. \
You cannot widen the stop or enlarge the size. Safe modifications are applied \
regardless of verdict, including approve and nonblocking reject.\
"""


def build_reviewer(reviewer: ReviewerSettings, settings: Settings) -> Reviewer:
    """Choose the operator-configured transport without resolving credentials here."""
    if reviewer.transport == "hermes":
        settings.require_reviewers()
        assert settings.hermes_home is not None
        assert reviewer.model is not None
        return HermesReviewer(
            reviewer.provider,
            reviewer.model,
            settings.hermes_python,
            settings.hermes_home,
            settings.reviewer_timeout_s,
        )
    return OpenAICompatReviewer.from_settings(reviewer, settings.reviewer_timeout_s)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON envelopes and nested verdicts with duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class HermesReviewer:
    """Tool-free inference in an operator-selected Hermes interpreter, not an agent loop."""

    def __init__(
        self, provider: str, model: str, python: Path | None, home: Path, timeout_s: float
    ) -> None:
        self._provider = provider
        self._model = model
        self._python = python
        self._home = home
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return self._provider

    def review(self, packet: ReviewPacket) -> ReviewOutcome:
        request = {
            "provider": self._provider,
            "model": self._model,
            "packet": packet.canonical_json(),
            "packet_sha256": packet.sha256,
            "system_prompt": SYSTEM_PROMPT,
            "timeout_s": self._timeout_s,
        }
        # Resolved per review: Hermes updates replace and prune its managed environments.
        python = resolve_hermes_python(self._python, self._home)
        if python is None:
            return ReviewOutcome(reviewer=self.name, error="Hermes interpreter not found")
        # Deliberate allowlist: no profile provider keys, Python import overrides,
        # proxy overrides or HERMES_PROFILE can cross this boundary.
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG") if key in os.environ}
        env["HERMES_HOME"] = str(self._home)
        deadline = time.monotonic() + 2 * self._timeout_s
        error = "Hermes inference failed"
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = subprocess.run(
                    [str(python), str(Path(__file__).with_name("hermes_inference.py"))],
                    input=json.dumps(request),
                    text=True,
                    capture_output=True,
                    cwd=str(self._home),
                    env=env,
                    timeout=min(self._timeout_s, remaining),
                    check=False,
                )
                if result.returncode != 0:
                    error = "Hermes process failed"
                    continue
                if len(result.stdout) > 65_536:
                    raise ValueError("oversized response")
                reply = json.loads(result.stdout, object_pairs_hook=_unique_object)
                if not isinstance(reply, dict) or set(reply) != {
                    "packet_sha256",
                    "provider",
                    "model",
                    "verdict",
                }:
                    raise ValueError("invalid envelope")
                if any(
                    reply[key] != request[key] for key in ("packet_sha256", "provider", "model")
                ):
                    raise ValueError("mismatched binding")
                verdict = ReviewVerdict.model_validate(reply["verdict"])
                return ReviewOutcome(reviewer=self.name, verdict=verdict)
            except Exception:
                # Never echo subprocess output, exceptions or Hermes credentials.
                error = "Hermes inference failed"
        return ReviewOutcome(reviewer=self.name, error=f"{error} (after one retry)")


class OpenAICompatReviewer:
    """A reviewer reached through an OpenAI-compatible chat completions API.

    Each review sends the packet with JSON output requested. An API error (timeouts
    included) or a reply that is not a valid verdict is retried once; a second
    failure gives an error outcome. It never raises for those.
    """

    def __init__(self, name: str, client: OpenAI, model: str) -> None:
        """Use `client` as it is; `from_settings` builds one with timeout and no SDK retries."""
        self._name = name
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, settings: ReviewerSettings, timeout_s: float) -> "OpenAICompatReviewer":
        """A reviewer named after its provider, with a `timeout_s` limit on each request.

        Raises ConfigError when the model id or the API key is missing. Makes no request.
        """
        if settings.model is None or settings.api_key is None:
            raise ConfigError(
                f"reviewer {settings.provider} needs a model id and {settings.api_key_env}"
            )
        client = OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=timeout_s,
            max_retries=0,
        )
        return cls(settings.provider, client, settings.model)

    @property
    def name(self) -> str:
        return self._name

    def review(self, packet: ReviewPacket) -> ReviewOutcome:
        error = ""
        for _attempt in range(2):
            try:
                completion = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": packet.canonical_json()},
                    ],
                    response_format={"type": "json_object"},
                )
                verdict = ReviewVerdict.model_validate_json(
                    completion.choices[0].message.content or ""
                )
                return ReviewOutcome(reviewer=self._name, verdict=verdict)
            except OpenAIError as exc:
                error = f"API error: {type(exc).__name__}: {exc}"
            except ValidationError as exc:
                error = f"invalid verdict: {exc.error_count()} error(s): {exc.errors()[0]['msg']}"
        return ReviewOutcome(reviewer=self._name, error=f"{error} (after one retry)")


def run_reviewers(reviewers: Sequence[Reviewer], packet: ReviewPacket) -> list[ReviewOutcome]:
    """Run every reviewer on `packet` at once, each in its own thread.

    Returns one outcome per reviewer, in `reviewers` order. A reviewer that raises
    yields an error outcome instead, so one broken reviewer blocks the buy rather than
    failing the call.
    """

    def review(reviewer: Reviewer) -> ReviewOutcome:
        try:
            return reviewer.review(packet)
        except Exception as exc:
            return ReviewOutcome(
                reviewer=reviewer.name, error=f"reviewer raised {type(exc).__name__}: {exc}"
            )

    with ThreadPoolExecutor(max_workers=max(len(reviewers), 1)) as pool:
        return list(pool.map(review, reviewers))


@dataclass(frozen=True)
class ReviewDecision:
    """What the reviews allow: the final order, or why the buy is blocked."""

    order: SizedOrder | None
    """The order with every accepted modification applied; None when blocked."""
    reasons: list[str] = field(default_factory=list)
    """Block reasons, nonblocking objections or applied modifications, naming each reviewer."""

    @property
    def proceed(self) -> bool:
        """True when the buy may go ahead, as `order`."""
        return self.order is not None


def decide(
    outcomes: Sequence[ReviewOutcome],
    sized: SizedOrder,
    portfolio: Portfolio,
    caps: Caps,
) -> ReviewDecision:
    """Apply the review rule to `sized`, the order the risk gate passed.

    Blocks on incomplete outcomes from the two distinct reviewers, any review failure,
    or any high-confidence reject. Low/medium-confidence rejections are nonblocking
    objections recorded in `reasons`. Otherwise applies the safer modifications of
    every verdict: the stop becomes the highest of the proposed stop and the requested
    stops below entry, and the size is multiplied by the smallest requested multiplier
    (at most 1). The order is re-sized with `size_position` at the new stop, and the
    final qty is floor(min(original qty, re-sized qty) * multiplier), so it never
    exceeds the original. Target changes are ignored (advisory). `portfolio` and
    `caps` must be the ones `sized` was sized with.
    """
    if len(outcomes) != 2 or len({outcome.reviewer for outcome in outcomes}) != 2:
        return ReviewDecision(
            order=None, reasons=["review: expected outcomes from two distinct reviewers"]
        )
    block_reasons = [
        f"{outcome.reviewer}: review failed: {outcome.error or 'missing verdict'}"
        for outcome in outcomes
        if outcome.error is not None or outcome.verdict is None
    ]
    verdicts = [(o.reviewer, o.verdict) for o in outcomes if o.verdict is not None]
    block_reasons += [
        f"{name}: rejected with high confidence: {'; '.join(verdict.reasons)}"
        for name, verdict in verdicts
        if verdict.verdict == "reject" and verdict.confidence == "high"
    ]
    if block_reasons:
        return ReviewDecision(order=None, reasons=block_reasons)

    objections = [
        f"{name}: nonblocking {verdict.confidence}-confidence objection: "
        f"{'; '.join(verdict.reasons)}"
        for name, verdict in verdicts
        if verdict.verdict == "reject"
    ]
    stop, stop_by = sized.stop_price, ""
    multiplier, multiplier_by = 1.0, ""
    for name, verdict in verdicts:
        changes = verdict.modifications
        if changes is None:
            continue
        if changes.stop_price is not None:
            requested = round_price(changes.stop_price)
            if stop < requested < sized.entry_price:
                stop, stop_by = requested, name
        if changes.size_multiplier is not None and changes.size_multiplier < multiplier:
            multiplier, multiplier_by = changes.size_multiplier, name
    # Only the changes that made the final order are reported; a superseded one is not.
    applied = [f"{stop_by}: stop raised to {stop}"] if stop_by else []
    if multiplier_by:
        applied.append(f"{multiplier_by}: size multiplied by {multiplier}")

    base = sized
    if stop != sized.stop_price:
        resized = size_position(
            sized.symbol, sized.entry_price, stop, sized.target_price, portfolio, caps
        )
        if resized.sized is None:
            reasons = [f"{stop_by}: raised stop {stop} fails sizing: {r}" for r in resized.reasons]
            return ReviewDecision(order=None, reasons=reasons)
        if resized.sized.qty < sized.qty:
            base = resized.sized

    qty = math.floor(base.qty * multiplier)
    if qty < 1:
        reason = (
            f"{multiplier_by}: size multiplier {multiplier} leaves no whole share of {base.qty}"
        )
        return ReviewDecision(order=None, reasons=[reason])
    order = SizedOrder.build(
        sized.symbol,
        qty,
        sized.entry_price,
        stop,
        sized.target_price,
        portfolio.account.equity,
        bound_by=sized.bound_by if qty == sized.qty else "review",
    )
    return ReviewDecision(order=order, reasons=objections + applied)
