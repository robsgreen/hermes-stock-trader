"""Fixtures for driving the MCP server in-process, the way Hermes calls it.

Each test talks to `build_server(...)` through `call_tool`, over the real service, risk
gate, review decision, state database (a temp file) and journal. Only the outside world
is faked: `FakeBroker` for Alpaca and `FakeReviewer` for the reviewer APIs.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError

from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.models import Bar, Clock, Modifications, Order, ReviewVerdict, TradingDay
from trader_broker.review import Reviewer
from trader_broker.server import build_server
from trader_broker.service import TraderService
from trader_broker.state import StateStore

ToolResult = dict[str, Any]

REFUSED = (ValidationError, ToolError)
"""What `call_tool` raises when FastMCP refuses the arguments before the tool runs."""

APPROVE = ReviewVerdict(verdict="approve", confidence="medium", reasons=["Sound setup."])


def call_tool(server: FastMCP, name: str, **arguments: Any) -> ToolResult:
    """Call tool `name` on `server` and return its JSON result."""
    result = asyncio.run(server.call_tool(name, arguments))
    assert result.structured_content is not None
    return result.structured_content


def buy_arguments(**overrides: Any) -> dict[str, Any]:
    """`propose_buy` arguments for XYZ: limit 100, stop 95, target 120."""
    return {
        "symbol": "XYZ",
        "stop_price": 95,
        "target_price": 120,
        "thesis": "Breakout above a six-month base on rising volume; earnings 2026-12-01.",
        "invalidation": "A close back inside the base.",
        "horizon_days": 20,
        "limit_price": 100,
        **overrides,
    }


@dataclass
class MarketTime:
    """The clock shared by the service and `FakeBroker`, moved forward by tests.

    Every weekday is a regular 09:30-16:00 New York session (14:30-21:00 UTC in
    November); `FakeBroker`'s clock and calendar follow the current time.
    """

    broker: FakeBroker
    current: datetime
    sessions: list[TradingDay] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.set(self.current)

    def __call__(self) -> datetime:
        return self.current

    def set(self, moment: datetime) -> None:
        self.current = moment
        session_open = moment.replace(hour=14, minute=30, second=0, microsecond=0)
        session_close = moment.replace(hour=21, minute=0, second=0, microsecond=0)
        self.broker.set_now(moment)
        self.broker.set_clock(
            Clock(
                timestamp=moment,
                is_open=session_open <= moment < session_close,
                next_open=session_open + timedelta(days=1),
                next_close=session_close,
            )
        )
        day = TradingDay(date=moment.date(), open=session_open, close=session_close)
        if day not in self.sessions:
            self.sessions.append(day)
        self.broker.set_calendar(self.sessions)

    def advance(self, **delta: float) -> None:
        self.set(self.current + timedelta(**delta))


@pytest.fixture
def market_time(fake_broker: FakeBroker, now: datetime) -> MarketTime:
    return MarketTime(fake_broker, now)


ServerFactory = Callable[..., FastMCP]


@pytest.fixture
def make_server(
    fake_broker: FakeBroker,
    state_store: StateStore,
    market_time: MarketTime,
    fake_reviewer: Callable[..., Reviewer],
) -> ServerFactory:
    """Builds a server: `make_server(reviewers=None, state=..., settings=Settings())`.

    Defaults to two approving reviewers, `openai` and `xai`, and the shared state file.
    """
    unset: Any = object()

    def build(
        reviewers: Sequence[Reviewer] | None = None,
        state: StateStore | None = unset,
        settings: Settings | None = None,
    ) -> FastMCP:
        if reviewers is None:
            reviewers = [fake_reviewer("openai", APPROVE), fake_reviewer("xai", APPROVE)]
        service = TraderService(
            fake_broker,
            state_store if state is unset else state,
            settings or Settings(),
            reviewers,
            now=market_time,
            sleep=lambda seconds: None,
        )
        return build_server(service)

    return build


@pytest.fixture
def server(make_server: ServerFactory) -> FastMCP:
    """A server with two approving reviewers over `fake_broker` and `state_store`."""
    return make_server()


def orders_for(broker: FakeBroker, symbol: str, status: str = "all") -> list[Order]:
    """Every order for `symbol` with `status` (`open`, `closed` or `all`), oldest first."""
    orders = broker.get_orders(status, limit=1000)  # type: ignore[arg-type]
    return [order for order in reversed(orders) if order.symbol == symbol]


def bracket_of(broker: FakeBroker, entry_id: str) -> tuple[Order, Order, Order]:
    """The entry, take-profit leg and stop leg of the bracket whose entry is `entry_id`."""
    orders = {order.id: order for order in broker.get_orders("all", limit=1000)}
    legs = [order for order in orders.values() if order.parent_id == entry_id]
    take_profit = next(leg for leg in legs if leg.type == "limit")
    stop = next(leg for leg in legs if leg.type == "stop")
    return orders[entry_id], take_profit, stop


def place_filled_buy(server: FastMCP, broker: FakeBroker, **overrides: Any) -> ToolResult:
    """`propose_buy` (XYZ, limit 100, stop 95 unless overridden), then fill the entry."""
    result = call_tool(server, "propose_buy", **buy_arguments(**overrides))
    assert result["status"] == "placed", result
    broker.fill_order(result["order"]["order_id"])
    return result


def at(hour: int, minute: int = 0, day: int = 3) -> datetime:
    """A UTC moment in November 2026."""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def daily_close(day: int, close: int) -> Bar:
    """A November 2026 daily bar closing at `close`, stamped as Alpaca does: midnight
    New York time (05:00 UTC in November)."""
    return Bar(
        timestamp=datetime(2026, 11, day, 5, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=50_000_000,
    )


def verdict(kind: str, confidence: str = "medium", **modifications: Any) -> ReviewVerdict:
    """A verdict of `kind` with one reason, asking for `modifications` when given."""
    return ReviewVerdict.model_validate(
        {
            "verdict": kind,
            "confidence": confidence,
            "reasons": [f"{kind} with {confidence} confidence"],
            "modifications": Modifications(**modifications) if modifications else None,
        }
    )
