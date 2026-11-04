"""The go-live scorecard: how the paper period has gone, scored against the go-live bar.

Everything is measured from the state database's `paper_start_date`: the account's
return and maximum drawdown from the broker's portfolio history, SPY's return over
the same days, the integrity violations reconciliation found, the buys the server
blocked, and how each reviewer's verdicts compare with how the reviewed trades did.
Passing every criterion unlocks nothing by itself; live mode is only ever set by hand.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from trader_broker.brokers.base import Broker
from trader_broker.market import (
    BENCHMARK_SYMBOL,
    MARKET_TZ,
    close_on_or_before,
    market_date,
    simple_return,
)
from trader_broker.models import Bar, BlockRule, EquityPoint, VerdictName
from trader_broker.state import Block, StateStore, StoredReview

GO_LIVE_WEEKS = 8
"""Paper trading must have run at least this many weeks."""

MAX_DRAWDOWN_PCT = Decimal("0.25")
"""The maximum drawdown must stay below this share of the peak equity."""

GO_LIVE_NOTE = (
    "Passing unlocks nothing by itself: live trading needs live keys and "
    "TRADER_BROKER_LIVE_ACK set by hand. Paper fills have no slippage, so treat a "
    "narrow beat of SPY with suspicion."
)


class _ReportPart(BaseModel):
    model_config = ConfigDict(frozen=True)


class Criterion(_ReportPart):
    """One line of the go-live bar and whether the paper period meets it."""

    name: Literal["time", "return", "drawdown", "integrity"]
    passed: bool
    detail: str
    """The measured value against the bar, in words."""


class ReviewerStats(_ReportPart):
    """How one reviewer answered, and how often its verdict matched the outcome."""

    reviewer: str
    verdicts: dict[VerdictName, int]
    """How many reviews the reviewer answered each way; every verdict is present."""
    settled_trades: int
    """Settled trades the reviewer gave a verdict on (approve, modify or reject)."""
    hits: int
    """Settled trades where the verdict matched the outcome: an approve or modify of a
    trade that beat SPY, or a reject of one that did not."""
    hit_rate: float | None
    """hits / settled_trades; None before any such trade has settled."""


class GoLiveReport(_ReportPart):
    """The scorecard. Percentages are shares of a whole (0.08 means 8%)."""

    paper_start_date: date
    as_of: date
    """Today's New York date when the report was built."""
    days_elapsed: int
    weeks_elapsed: float
    """days_elapsed / 7, to one decimal."""
    portfolio_return_pct: float | None
    """Equity change over the portfolio history; None without two funded points."""
    spy_return_pct: float | None
    """SPY's close-to-close return from the start date; None without the closes."""
    max_drawdown_pct: float | None
    """Largest fall from a running equity peak; None without portfolio history."""
    integrity_violations: int
    blocks_by_reason: dict[BlockRule, int]
    """Blocked buys per rule id (`kill_switch`, `broker_error`, `review` for a review
    block, ...). A block with several rules counts once under each."""
    reviewers: list[ReviewerStats]
    """One entry per reviewer that answered a review, by name."""
    criteria: list[Criterion]
    passed: bool
    """True when every criterion passed."""
    note: str = GO_LIVE_NOTE

    def as_text(self) -> str:
        """The report for a person to read, holding the same values as the JSON form."""
        lines = [
            f"Go-live report for paper trading since {self.paper_start_date} (as of {self.as_of})",
            "",
        ]
        lines += [
            f"{'PASS' if c.passed else 'FAIL'}  {c.name:<9}  {c.detail}" for c in self.criteria
        ]
        failed = sum(not c.passed for c in self.criteria)
        overall = "PASS" if self.passed else f"FAIL ({failed} of {len(self.criteria)} failed)"
        lines += ["", f"Overall: {overall}", self.note, "", "Blocks by reason:"]
        lines += [f"  {reason}: {count}" for reason, count in self.blocks_by_reason.items()]
        if not self.blocks_by_reason:
            lines.append("  none")
        lines += ["", "Reviewers:"]
        lines += [f"  {_reviewer_line(stats)}" for stats in self.reviewers]
        if not self.reviewers:
            lines.append("  none")
        return "\n".join(lines)


def build_report(state: StateStore, broker: Broker, now: datetime) -> GoLiveReport:
    """Measure the paper period from `state`'s paper start date up to `now`.

    Reads the broker's portfolio history and SPY's daily bars, and the integrity
    violations, blocks, reviews and settled journal entries from `state`. A criterion
    that cannot be measured yet (no portfolio history, no SPY close) fails. Raises
    `BrokerError` when the broker cannot be read and `StateUnavailableError` when
    state cannot.
    """
    start = state.paper_start_date()
    today = market_date(now)
    since = datetime.combine(start, time(), MARKET_TZ)
    days_elapsed = (today - start).days

    equity = [point for point in broker.get_portfolio_history(start) if point.equity > 0]
    portfolio_return = (
        simple_return(equity[0].equity, equity[-1].equity) if len(equity) > 1 else None
    )
    drawdown = _max_drawdown(equity)
    # A session per calendar day is more than enough to reach back to the start date.
    spy_return = _spy_return(broker.get_daily_bars(BENCHMARK_SYMBOL, days_elapsed + 1), start)
    violations = state.integrity_violation_count()

    criteria = [
        Criterion(
            name="time",
            passed=days_elapsed >= GO_LIVE_WEEKS * 7,
            detail=f"{days_elapsed / 7:.1f} weeks of paper trading (need at least {GO_LIVE_WEEKS})",
        ),
        Criterion(
            name="return",
            passed=(
                portfolio_return is not None
                and spy_return is not None
                and portfolio_return > spy_return
            ),
            detail=f"portfolio {_pct(portfolio_return)} vs SPY {_pct(spy_return)}"
            " (need to beat SPY)",
        ),
        Criterion(
            name="drawdown",
            passed=drawdown is not None and drawdown < MAX_DRAWDOWN_PCT,
            detail=f"max drawdown {_pct(drawdown, signed=False)}"
            f" (need under {MAX_DRAWDOWN_PCT:.0%})",
        ),
        Criterion(
            name="integrity",
            passed=violations == 0,
            detail=f"{violations} integrity violations (need 0)",
        ),
    ]
    return GoLiveReport(
        paper_start_date=start,
        as_of=today,
        days_elapsed=days_elapsed,
        weeks_elapsed=round(days_elapsed / 7, 1),
        portfolio_return_pct=_float_or_none(portfolio_return),
        spy_return_pct=_float_or_none(spy_return),
        max_drawdown_pct=_float_or_none(drawdown),
        integrity_violations=violations,
        blocks_by_reason=_blocks_by_reason(state.blocks_since(since)),
        reviewers=_reviewer_stats(state, state.reviews_since(since)),
        criteria=criteria,
        passed=all(criterion.passed for criterion in criteria),
    )


def _max_drawdown(equity: Sequence[EquityPoint]) -> Decimal | None:
    """The largest fall from a running peak of `equity`, as a share of that peak."""
    if not equity:
        return None
    peak, worst = equity[0].equity, Decimal(0)
    for point in equity:
        peak = max(peak, point.equity)
        worst = max(worst, (peak - point.equity) / peak)
    return worst


def _spy_return(bars: Sequence[Bar], start: date) -> Decimal | None:
    """SPY's return from its last close on or before `start` to its latest close."""
    start_close = close_on_or_before(bars, start)
    if start_close is None:
        return None
    return simple_return(start_close, bars[-1].close)


def _blocks_by_reason(blocks: Sequence[Block]) -> dict[BlockRule, int]:
    """Count `blocks` per stored rule id, most frequent first."""
    counts: Counter[BlockRule] = Counter()
    for block in blocks:
        counts.update(set(block.rules))
    return dict(counts.most_common())


def _reviewer_stats(state: StateStore, reviews: Sequence[StoredReview]) -> list[ReviewerStats]:
    """Each reviewer's verdict counts over `reviews`, and its hit rate on settled trades.

    A settled trade is a closed journal entry with an `alpha_pct`; it is scored against
    the review its `review_id` names. Reviews that blocked the buy have no trade.
    """
    beat_spy = {
        entry.review_id: entry.alpha_pct > 0
        for entry in state.journal_entries("closed", limit=None)
        if entry.review_id is not None and entry.alpha_pct is not None
    }
    verdicts: dict[str, Counter[VerdictName]] = {}
    scored: Counter[str] = Counter()
    hits: Counter[str] = Counter()
    for review in reviews:
        for outcome in review.outcomes:
            verdict = outcome.verdict_name
            verdicts.setdefault(outcome.reviewer, Counter())[verdict] += 1
            if verdict == "error" or review.id not in beat_spy:
                continue
            scored[outcome.reviewer] += 1
            hits[outcome.reviewer] += (verdict != "reject") == beat_spy[review.id]
    return [
        ReviewerStats(
            reviewer=name,
            verdicts={verdict: counts[verdict] for verdict in get_args(VerdictName)},
            settled_trades=scored[name],
            hits=hits[name],
            hit_rate=hits[name] / scored[name] if scored[name] else None,
        )
        for name, counts in sorted(verdicts.items())
    ]


def _reviewer_line(stats: ReviewerStats) -> str:
    counts = ", ".join(f"{verdict} {count}" for verdict, count in stats.verdicts.items())
    if stats.hit_rate is None:
        return f"{stats.reviewer}: {counts}; no settled trades yet"
    return (
        f"{stats.reviewer}: {counts}; hit rate {stats.hit_rate:.0%}"
        f" ({stats.hits} of {stats.settled_trades} settled trades)"
    )


def _pct(share: Decimal | None, signed: bool = True) -> str:
    """`share` as a percentage with two decimals, signed unless not `signed`; n/a if None."""
    if share is None:
        return "n/a"
    return f"{share:+.2%}" if signed else f"{share:.2%}"


def _float_or_none(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
