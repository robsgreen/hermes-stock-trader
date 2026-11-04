"""US market conventions the server shares: the trading date, the benchmark and returns."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader_broker.models import Bar

MARKET_TZ = ZoneInfo("America/New_York")
"""Time zone of the US stock market; its calendar date is the trading date."""

BENCHMARK_SYMBOL = "SPY"
"""The market benchmark: every closed trade and the paper period are compared with it,
and its trend is the market regime the reviewers see."""


def market_date(moment: datetime) -> date:
    """The trading date `moment` (timezone-aware) falls on: its New York calendar date."""
    return moment.astimezone(MARKET_TZ).date()


def simple_return(start: Decimal, end: Decimal) -> Decimal:
    """The return from price `start` to price `end`, as a share of `start` (0.1 for +10%)."""
    return (end - start) / start


def close_on_or_before(bars: Sequence[Bar], day: date) -> Decimal | None:
    """The close of the last of `bars` (daily, oldest first) whose trading date is on or
    before `day`; None when there is none."""
    closes = [bar.close for bar in bars if market_date(bar.timestamp) <= day]
    return closes[-1] if closes else None
