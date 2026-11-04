"""The `Broker` interface: everything the server needs from a brokerage.

Adapters live in this package and are the only code that knows a broker's SDK. The
rest of the server talks to a `Broker` and handles the error types defined here.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from trader_broker.models import (
    Account,
    Asset,
    Bar,
    BracketOrderRequest,
    Clock,
    EquityPoint,
    Fill,
    Movers,
    NewsItem,
    Order,
    Position,
    Quote,
    TradingDay,
)


class BrokerError(Exception):
    """The broker refused a request or could not be reached."""


class RateLimitError(BrokerError):
    """The broker rejected a request for exceeding its rate limit (HTTP 429)."""


class UnknownResultError(BrokerError):
    """A write may or may not have taken effect (for example, it timed out).

    Look the order up by its client order id before deciding to retry.
    """


@runtime_checkable
class Broker(Protocol):
    """A brokerage account. Every method raises `BrokerError` (or a subclass) on failure."""

    def get_account(self) -> Account: ...

    def get_positions(self) -> list[Position]: ...

    def get_orders(
        self, status: Literal["open", "closed", "all"] = "open", limit: int = 100
    ) -> list[Order]:
        """Newest orders first, as a flat list: bracket legs appear as their own
        orders with `parent_id` set to the entry's id. `open` keeps every order, leg or
        not, whose own status is open (`Order.is_open`)."""
        ...

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        """The order with this client order id, or None if the broker has none."""
        ...

    def get_asset(self, symbol: str) -> Asset | None:
        """The asset, or None if the broker does not know the symbol."""
        ...

    def get_quote(self, symbol: str) -> Quote: ...

    def get_daily_bars(self, symbol: str, days: int, *, sip_history: bool = True) -> list[Bar]:
        """Up to `days` daily bars, oldest first, ending at the previous close.

        `sip_history` asks for consolidated (all-exchange) volume where the data
        plan allows it only for history, rather than a single exchange's feed.
        """
        ...

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        """The latest `limit` bars of `timeframe` (`1Min`, `1Hour`, `1Day`...), oldest first."""
        ...

    def get_news(self, symbols: list[str] | None, limit: int) -> list[NewsItem]:
        """Newest headlines first, about any of `symbols` (None: all news)."""
        ...

    def get_movers(self, limit: int) -> Movers:
        """Up to `limit` top gainers and `limit` top losers of the day."""
        ...

    def get_clock(self) -> Clock: ...

    def get_calendar(self, start: date, end: date) -> list[TradingDay]:
        """Trading sessions from `start` to `end`, inclusive; days without a session are absent."""
        ...

    def get_portfolio_history(self, start: date) -> list[EquityPoint]:
        """Daily account equity from `start` to now, oldest first."""
        ...

    def get_fills(self, since: datetime) -> list[Fill]:
        """Fills at or after `since`, oldest first."""
        ...

    def submit_bracket_buy(self, req: BracketOrderRequest) -> Order:
        """Submit the entry with its stop-loss and take-profit legs; returns the entry."""
        ...

    def submit_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        """Submit a market sell, good for the day."""
        ...

    def submit_stop(
        self, symbol: str, qty: int, stop_price: Decimal, client_order_id: str
    ) -> Order:
        """Submit a stop sell, good until cancelled."""
        ...

    def replace_stop(self, order_id: str, stop_price: Decimal, client_order_id: str) -> Order:
        """Move an open stop order to `stop_price`; returns the replacement order, which
        carries `client_order_id`."""
        ...

    def cancel_order(self, order_id: str) -> None:
        """Request cancellation of an open order. The broker may cancel asynchronously:
        the order can stay listed as open (`pending_cancel`) for a while."""
        ...
