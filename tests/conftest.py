"""Shared pytest fixtures."""

import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.models import (
    Account,
    Asset,
    Bar,
    Clock,
    NewsItem,
    Position,
    Proposal,
    Quote,
    ReviewOutcome,
    ReviewVerdict,
    TradingDay,
)
from trader_broker.review import ReviewPacket, build_packet
from trader_broker.risk import Caps, Portfolio, size_position
from trader_broker.service import TraderService
from trader_broker.state import StateStore, init_state

_SETTINGS_PREFIXES = ("TRADER_", "REVIEWER_", "ALPACA_")
_SETTINGS_NAMES = ("OPENAI_API_KEY", "XAI_API_KEY")


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable the server reads, so the developer's shell cannot leak in."""
    for name in list(os.environ):
        if name.startswith(_SETTINGS_PREFIXES) or name in _SETTINGS_NAMES:
            monkeypatch.delenv(name)
    yield


class FakeReviewer:
    """A `Reviewer` that answers every review the same scripted way.

    `answer` is the verdict to return, an exception to raise, or "invalid_json" for the
    error outcome a real reviewer gives after its retry also got an unparseable reply.
    `delay_s` is slept before answering. Every packet reviewed is kept in `packets`.
    """

    def __init__(
        self, name: str, answer: ReviewVerdict | Exception | str, delay_s: float = 0.0
    ) -> None:
        self.name = name
        self.answer = answer
        self.delay_s = delay_s
        self.packets: list[ReviewPacket] = []

    def review(self, packet: ReviewPacket) -> ReviewOutcome:
        self.packets.append(packet)
        time.sleep(self.delay_s)
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, ReviewVerdict):
            return ReviewOutcome(reviewer=self.name, verdict=self.answer)
        return ReviewOutcome(reviewer=self.name, error="invalid verdict: Invalid JSON")


@pytest.fixture
def fake_reviewer() -> Callable[..., FakeReviewer]:
    """Builds `FakeReviewer`s: `fake_reviewer(name, answer, delay_s=0.0)`."""
    return FakeReviewer


@pytest.fixture
def review_packet(now: datetime) -> ReviewPacket:
    """A packet for buying `XYZ` at 100 with stop 95 and target 120.

    The account has $100k equity: $80k cash and a $20k `ABC` position. `XYZ` and SPY
    each have 260 flat daily bars; there are 12 headlines, an hour apart.
    """
    account = Account(
        equity=Decimal(100_000),
        last_equity=Decimal(100_000),
        cash=Decimal(80_000),
        buying_power=Decimal(80_000),
    )
    abc = Position(
        symbol="ABC",
        qty=200,
        avg_entry_price=Decimal(90),
        current_price=Decimal(100),
        market_value=Decimal(20_000),
    )
    portfolio = Portfolio(account, [abc])
    proposal = Proposal(
        symbol="XYZ",
        stop_price=Decimal(95),
        target_price=Decimal(120),
        thesis="Breakout above a six-month base on rising volume.",
        invalidation="A close back inside the base.",
        horizon_days=20,
        limit_price=Decimal(100),
    )
    decision = size_position(
        "XYZ",
        Decimal(100),
        proposal.stop_price,
        proposal.target_price,
        portfolio,
        Caps.from_settings(Settings()),
    )
    assert decision.sized is not None
    bars = _daily_bars(ending_before=now, count=260)
    news = [
        NewsItem(
            id=f"news-{hour}",
            headline=f"XYZ headline {hour}",
            source="benzinga",
            symbols=["XYZ"],
            created_at=now - timedelta(hours=hour),
        )
        for hour in range(12)
    ]
    quote = Quote(
        symbol="XYZ", bid_price=Decimal("99.98"), ask_price=Decimal("100.02"), timestamp=now
    )
    return build_packet(proposal, decision.sized, quote, bars, news, portfolio, spy_bars=bars)


@pytest.fixture
def now() -> datetime:
    """Tuesday 2026-11-03, 10:30 New York time: an hour into a regular session."""
    return datetime(2026, 11, 3, 15, 30, tzinfo=UTC)


@pytest.fixture
def fake_broker(now: datetime) -> FakeBroker:
    """A $100k cash account with no positions, an open market and one liquid symbol.

    `XYZ` is an active, tradable US equity quoted 99.98 / 100.02, with 260 daily bars
    (close 100, volume 1M, so $100M average dollar volume) ending at the previous close;
    SPY has the same bars, for the review packet's market regime. The session runs
    14:30-21:00 UTC (09:30-16:00 New York).
    """
    session_open = now.replace(hour=14, minute=30)
    session_close = now.replace(hour=21, minute=0)
    broker = FakeBroker(now)
    broker.set_account(cash=Decimal(100_000))
    broker.set_clock(
        Clock(
            timestamp=now,
            is_open=True,
            next_open=session_open + timedelta(days=1),
            next_close=session_close,
        )
    )
    broker.set_calendar([TradingDay(date=now.date(), open=session_open, close=session_close)])
    broker.set_asset(
        Asset(
            symbol="XYZ", asset_class="us_equity", exchange="NASDAQ", status="active", tradable=True
        )
    )
    broker.set_quote("XYZ", bid=Decimal("99.98"), ask=Decimal("100.02"))
    broker.set_daily_bars("XYZ", _daily_bars(ending_before=session_open, count=260))
    broker.set_daily_bars("SPY", _daily_bars(ending_before=session_open, count=260))
    return broker


@pytest.fixture
def state_store(tmp_path: Path, now: datetime) -> Iterator[StateStore]:
    """A fresh paper state database: peak $100k, no halts, paper start on `now`'s date."""
    with init_state(tmp_path / "paper" / "state.db", Decimal(100_000), today=now.date()) as store:
        yield store


@pytest.fixture
def trader_service(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> TraderService:
    """A `TraderService` over `fake_broker` and `state_store`, with default settings, no
    reviewers, and its clock fixed at `now`."""
    return TraderService(fake_broker, state_store, Settings(), reviewers=[], now=lambda: now)


@pytest.fixture
def missing_state_path(tmp_path: Path) -> Path:
    """Where a state database would be, in a directory that does not exist yet."""
    return tmp_path / "missing" / "paper" / "state.db"


def _daily_bars(ending_before: datetime, count: int) -> list[Bar]:
    """`count` flat weekday bars (close 100, volume 1M), oldest first, before `ending_before`."""
    days: list[datetime] = []
    day = ending_before - timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [
        Bar(
            timestamp=day,
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=1_000_000,
        )
        for day in reversed(days)
    ]
