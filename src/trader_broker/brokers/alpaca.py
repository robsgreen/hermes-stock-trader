"""`Broker` backed by Alpaca through `alpaca-py`: the only module that imports `alpaca`.

Every SDK call goes through `_read` or `_write`, which turn SDK and transport failures
into the broker error types. Reads retry an HTTP 429 three times with 1 s, 2 s and
4 s pauses. Writes never retry: a write whose outcome is unknown (timeout, 5xx,
dropped connection, unreadable reply) raises `UnknownResultError`, so the caller
looks the order up by its client order id instead of submitting it again.
"""

import functools
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from time import sleep
from typing import TYPE_CHECKING, Literal, cast

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.common.rest import RESTClient
from alpaca.common.types import RawData
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar as SdkBar
from alpaca.data.models.quotes import Quote as SdkQuote
from alpaca.data.models.screener import Mover as SdkMover
from alpaca.data.requests import (
    MarketMoversRequest,
    NewsRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order as SdkOrder
from alpaca.trading.models import TradeActivity
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
)
from pydantic import ValidationError

from trader_broker.brokers.base import Broker, BrokerError, RateLimitError, UnknownResultError
from trader_broker.config import ConfigError, Settings
from trader_broker.market import MARKET_TZ, market_date
from trader_broker.models import (
    Account,
    Asset,
    Bar,
    BracketOrderRequest,
    Clock,
    EquityPoint,
    Fill,
    Mover,
    Movers,
    NewsItem,
    Order,
    Position,
    Quote,
    TradingDay,
    round_price,
)

_READ_RETRY_DELAYS_S = (1, 2, 4)
"""Pauses before each retry of a rate-limited read."""
_HTTP_TIMEOUT_S = 20
"""Longest wait for Alpaca to connect or to send each part of a reply."""
_SIP_HISTORY_DELAY = timedelta(minutes=16)
"""The free data plan serves SIP data only for windows ending at least 15 minutes ago."""
_HISTORY_START = datetime(2016, 1, 1, tzinfo=UTC)
"""Earlier than any Alpaca market data, so a newest-first query with a limit gets the
latest bars however far back they reach."""
_FILLS_PAGE_SIZE = 100
_NO_ACCOUNT_ID = uuid.UUID(int=0)
"""Stands in for the `account_id` Alpaca leaves out of some fill rows; nothing reads it."""
_OPEN_ORDER_SCAN = 500
"""Fewest orders each of the two reads behind `get_orders("open")` asks for (Alpaca's
maximum page size)."""
_TIMEFRAME = re.compile(r"(\d+)(Min|Hour|Day|Week|Month)")
_ORDER_STATUS_ALIASES = {"pending_review": "pending_new"}
"""Alpaca statuses that `models.OrderStatus` lacks, mapped to the nearest one it has."""


class _NotFoundError(BrokerError):
    """Alpaca answered 404: it has no such order or asset."""


class AlpacaBroker:
    """An Alpaca brokerage account: paper or live, as `Settings.mode` says.

    Build it with `from_settings`. Quotes and intraday bars come from the IEX feed,
    which the free data plan serves in real time.
    """

    def __init__(
        self,
        trading: TradingClient,
        stock_data: StockHistoricalDataClient,
        news: NewsClient,
        screener: ScreenerClient,
    ) -> None:
        """Use these SDK clients as they are; `from_settings` builds configured ones."""
        self._trading = trading
        self._stock_data = stock_data
        self._news = news
        self._screener = screener

    @classmethod
    def from_settings(cls, settings: Settings) -> "AlpacaBroker":
        """Connect to Alpaca's paper or live API with the keys in `settings`.

        Raises ConfigError when either Alpaca key is missing. Makes no request.
        """
        if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY must both be set")
        api_key = settings.alpaca_api_key.get_secret_value()
        secret_key = settings.alpaca_secret_key.get_secret_value()
        clients = (
            TradingClient(api_key, secret_key, paper=settings.mode == "paper"),
            StockHistoricalDataClient(api_key, secret_key),
            NewsClient(api_key, secret_key),
            ScreenerClient(api_key, secret_key),
        )
        for client in clients:
            _take_over_retries_and_timeouts(client)
        return cls(*clients)

    # ----- Reads -----

    def get_account(self) -> Account:
        def call() -> Account:
            account = _sdk(self._trading.get_account())
            return Account(
                equity=_decimal(account.equity),
                last_equity=_decimal(account.last_equity),
                cash=_decimal(account.cash),
                buying_power=_decimal(account.buying_power),
            )

        return _read(call)

    def get_positions(self) -> list[Position]:
        def call() -> list[Position]:
            return [
                Position(
                    symbol=position.symbol,
                    qty=int(_decimal(position.qty)),
                    avg_entry_price=_decimal(position.avg_entry_price),
                    current_price=_decimal(position.current_price),
                    market_value=_decimal(position.market_value),
                )
                for position in _sdk(self._trading.get_all_positions())
            ]

        return _read(call)

    def get_orders(
        self, status: Literal["open", "closed", "all"] = "open", limit: int = 100
    ) -> list[Order]:
        """Newest orders first, as a flat list: bracket legs follow their entry, with
        `parent_id` set. `limit` counts an entry and its legs as one order, as Alpaca does.

        `open` merges two reads, each of at least `_OPEN_ORDER_SCAN` orders: Alpaca's
        own `status=open` list, which reaches open orders of any age (an old GTC stop),
        and the newest orders of every status, which hold the working legs of a filled
        entry whether or not Alpaca's open list includes them. Orders in both count
        once, and each order is judged by its own status, legs included.
        """

        def read(query: QueryOrderStatus, count: int) -> list[SdkOrder]:
            request = GetOrdersRequest(status=query, limit=count, nested=True, direction=Sort.DESC)
            return list(_sdk(self._trading.get_orders(request)))

        def call() -> list[Order]:
            if status != "open":
                sdk_orders = read(QueryOrderStatus(status), limit)
            else:
                count = max(limit, _OPEN_ORDER_SCAN)
                merged = {
                    sdk_order.id: sdk_order
                    for sdk_order in read(QueryOrderStatus.OPEN, count)
                    + read(QueryOrderStatus.ALL, count)
                }
                sdk_orders = sorted(
                    merged.values(), key=lambda sdk_order: sdk_order.created_at, reverse=True
                )
            orders = [order for sdk_order in sdk_orders for order in _flatten(sdk_order)]
            if status == "all":
                return orders
            # A nested entry can carry legs in another state than its own (a filled entry
            # with working legs); keep only the orders in the state asked for.
            return [order for order in orders if order.is_open == (status == "open")]

        return _read(call)

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            return _read(
                lambda: _order(_sdk(self._trading.get_order_by_client_id(client_order_id)))
            )
        except _NotFoundError:
            return None

    def get_asset(self, symbol: str) -> Asset | None:
        def call() -> Asset:
            asset = _sdk(self._trading.get_asset(symbol))
            return Asset(
                symbol=asset.symbol,
                asset_class=asset.asset_class.value,
                exchange=asset.exchange.value,
                status=asset.status.value,
                tradable=asset.tradable,
            )

        try:
            return _read(call)
        except _NotFoundError:
            return None

    def get_quote(self, symbol: str) -> Quote:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)

        def call() -> Quote:
            # Both arms of the SDK's return type are dicts; outside raw-data mode it is this one.
            quotes = cast(dict[str, SdkQuote], self._stock_data.get_stock_latest_quote(request))
            if symbol not in quotes:
                raise BrokerError(f"Alpaca has no quote for {symbol}")
            quote = quotes[symbol]
            return Quote(
                symbol=symbol,
                bid_price=_decimal(quote.bid_price),
                ask_price=_decimal(quote.ask_price),
                timestamp=quote.timestamp,
            )

        return _read(call)

    def get_daily_bars(self, symbol: str, days: int, *, sip_history: bool = True) -> list[Bar]:
        """Up to `days` split-adjusted daily bars, oldest first, ending at the previous close.

        With `sip_history`, bars come from the consolidated SIP feed, which the free
        plan serves for history; otherwise from IEX.
        """
        now = datetime.now(UTC)
        end = _start_of_market_day(now) - timedelta(microseconds=1)
        if sip_history:
            end = min(end, now - _SIP_HISTORY_DELAY)
        feed = DataFeed.SIP if sip_history else DataFeed.IEX
        return self._latest_bars(symbol, TimeFrame.Day, days, feed, end)

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        """The latest `limit` split-adjusted IEX bars of `timeframe`, oldest first.

        `timeframe` is an amount and a unit: `Min`, `Hour`, `Day`, `Week` or `Month`
        (`5Min`, `1Hour`, `1Day`...). Raises BrokerError for any other form.
        """
        match = _TIMEFRAME.fullmatch(timeframe)
        if match is None:
            raise BrokerError(f"unsupported bar timeframe: {timeframe!r}")
        try:
            frame = TimeFrame(int(match[1]), TimeFrameUnit(match[2]))
        except ValueError as exc:
            raise BrokerError(f"unsupported bar timeframe: {timeframe!r} ({exc})") from exc
        return self._latest_bars(symbol, frame, limit, DataFeed.IEX, end=None)

    def get_news(self, symbols: list[str] | None, limit: int) -> list[NewsItem]:
        if symbols == []:
            return []
        request = NewsRequest(
            symbols=None if symbols is None else ",".join(symbols), limit=limit, sort="desc"
        )

        def call() -> list[NewsItem]:
            news = _sdk(self._news.get_news(request))
            return [
                NewsItem(
                    id=str(article.id),
                    headline=article.headline,
                    summary=article.summary,
                    source=article.source,
                    symbols=article.symbols,
                    url=article.url or None,
                    created_at=article.created_at,
                )
                for article in news.data.get("news", [])
            ]

        return _read(call)

    def get_movers(self, limit: int) -> Movers:
        def call() -> Movers:
            movers = _sdk(self._screener.get_market_movers(MarketMoversRequest(top=limit)))
            return Movers(
                gainers=[_mover(mover) for mover in movers.gainers],
                losers=[_mover(mover) for mover in movers.losers],
            )

        return _read(call)

    def get_clock(self) -> Clock:
        def call() -> Clock:
            clock = _sdk(self._trading.get_clock())
            return Clock(
                timestamp=clock.timestamp,
                is_open=clock.is_open,
                next_open=clock.next_open,
                next_close=clock.next_close,
            )

        return _read(call)

    def get_calendar(self, start: date, end: date) -> list[TradingDay]:
        request = GetCalendarRequest(start=start, end=end)

        def call() -> list[TradingDay]:
            # The SDK reads Alpaca's session times as naive New York times.
            return [
                TradingDay(
                    date=day.date,
                    open=day.open.replace(tzinfo=MARKET_TZ),
                    close=day.close.replace(tzinfo=MARKET_TZ),
                )
                for day in _sdk(self._trading.get_calendar(request))
            ]

        return _read(call)

    def get_portfolio_history(self, start: date) -> list[EquityPoint]:
        request = GetPortfolioHistoryRequest(
            start=datetime.combine(start, time(), tzinfo=MARKET_TZ), timeframe="1D"
        )

        def call() -> list[EquityPoint]:
            history = _sdk(self._trading.get_portfolio_history(request))
            return [
                EquityPoint(timestamp=datetime.fromtimestamp(stamp, UTC), equity=_decimal(equity))
                for stamp, equity in zip(history.timestamp, history.equity, strict=True)
            ]

        return _read(call)

    def get_fills(self, since: datetime) -> list[Fill]:
        """Fills at or after `since`, oldest first, read page by page from account activities."""
        return [
            fill for page in self._fill_pages(since) for fill in page if fill.filled_at >= since
        ]

    # ----- Writes -----

    def submit_bracket_buy(self, req: BracketOrderRequest) -> Order:
        """Submit the limit entry with its take-profit and stop-loss legs; returns the entry."""
        payload = {
            "symbol": req.symbol,
            "qty": str(req.qty),
            "side": "buy",
            "type": "limit",
            "order_class": "bracket",
            "time_in_force": req.time_in_force,
            "limit_price": _price(req.limit_price),
            "take_profit": {"limit_price": _price(req.take_profit_price)},
            "stop_loss": {"stop_price": _price(req.stop_price)},
            "client_order_id": req.client_order_id,
        }
        return self._submit(payload)

    def submit_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        return self._submit(payload)

    def submit_stop(
        self, symbol: str, qty: int, stop_price: Decimal, client_order_id: str
    ) -> Order:
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": _price(stop_price),
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        }
        return self._submit(payload)

    def replace_stop(self, order_id: str, stop_price: Decimal, client_order_id: str) -> Order:
        """Move an open stop to `stop_price`; returns the replacement order.

        The replacement carries `client_order_id`. Alpaca's reply does not say whether
        it is a bracket leg, so its `parent_id` is None; read `get_orders` to see which
        entry it belongs to.
        """
        path = f"/orders/{_order_id(order_id)}"
        payload = {"stop_price": _price(stop_price), "client_order_id": client_order_id}
        return _write(lambda: _order(SdkOrder(**_json_object(self._trading.patch(path, payload)))))

    def cancel_order(self, order_id: str) -> None:
        checked_id = _order_id(order_id)
        _write(lambda: self._trading.cancel_order_by_id(checked_id))

    # ----- Internals -----

    def _submit(self, payload: RawData) -> Order:
        """POST an order as JSON with string prices, as Alpaca's order API documents them."""
        return _write(
            lambda: _order(SdkOrder(**_json_object(self._trading.post("/orders", payload))))
        )

    def _latest_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        limit: int,
        feed: DataFeed,
        end: datetime | None,
    ) -> list[Bar]:
        """The latest `limit` bars ending at `end` (None: now), oldest first."""
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=_HISTORY_START,
            end=end,
            limit=limit,
            sort=Sort.DESC,
            feed=feed,
            adjustment=Adjustment.SPLIT,
        )

        def call() -> list[Bar]:
            bars = _sdk(self._stock_data.get_stock_bars(request)).data.get(symbol, [])
            return [_bar(bar) for bar in reversed(bars)]

        return _read(call)

    def _fill_pages(self, since: datetime) -> Iterator[list[Fill]]:
        """Pages of fills from just before `since`, oldest first, until a short page."""
        params: RawData = {
            # Alpaca's `after` is exclusive; start a second early and filter exactly.
            "after": (since - timedelta(seconds=1)).isoformat(),
            "direction": "asc",
            "page_size": _FILLS_PAGE_SIZE,
        }
        while True:
            page = _read(functools.partial(self._fill_page, dict(params)))
            yield page
            if len(page) < _FILLS_PAGE_SIZE:
                return
            params["page_token"] = page[-1].id

    def _fill_page(self, params: RawData) -> list[Fill]:
        activities = self._trading.get("/account/activities/FILL", params)
        if not isinstance(activities, list):
            raise ValueError(f"expected a list of activities, got {type(activities).__name__}")
        return [_fill_from_row(index, activity) for index, activity in enumerate(activities)]


def _take_over_retries_and_timeouts(client: RESTClient) -> None:
    """Switch off the SDK's own retries and give every request a timeout.

    The SDK retries a 429 or 504 three times, writes included, and waits forever for
    a reply. `alpaca-py` 0.44 offers no public setting for either on these clients,
    so this sets its private attributes.
    """
    client._retry = 0
    session = client._session
    session.request = functools.partial(  # type: ignore[method-assign]
        session.request, timeout=_HTTP_TIMEOUT_S
    )


def _read[T](call: Callable[[], T]) -> T:
    """Run a read, retrying an HTTP 429 after each pause in `_READ_RETRY_DELAYS_S`."""
    for delay_s in _READ_RETRY_DELAYS_S:
        try:
            return _call(call, write=False)
        except RateLimitError:
            sleep(delay_s)
    return _call(call, write=False)


def _write[T](call: Callable[[], T]) -> T:
    """Run a write once; any failure that leaves its outcome open is `UnknownResultError`."""
    return _call(call, write=True)


def _call[T](call: Callable[[], T], *, write: bool) -> T:
    """Run `call`, turning SDK, transport and reply-parsing failures into broker errors."""
    try:
        return call()
    except APIError as exc:
        status = exc.status_code
        message = f"Alpaca HTTP {status}: {_api_message(exc)}"
        if status == 429:
            raise RateLimitError(message) from exc
        if status == 404:
            raise _NotFoundError(message) from exc
        if write and (status is None or status >= 500):
            raise UnknownResultError(message) from exc
        raise BrokerError(message) from exc
    except (OSError, ValueError) as exc:
        # Transport failures (timeouts, dropped connections) are OSErrors; replies that
        # do not parse into the models are ValueErrors.
        message = f"Alpaca request failed: {exc!r}"
        if write:
            raise UnknownResultError(message) from exc
        raise BrokerError(message) from exc


def _api_message(exc: APIError) -> str:
    """Alpaca's error message, or the raw reply when it is not Alpaca's JSON error shape."""
    try:
        return str(exc.message)
    except (ValueError, KeyError, TypeError):
        return str(exc)


def _sdk[T](result: T | RawData) -> T:
    """Narrow an SDK return value to its model; the clients never run in raw-data mode."""
    if isinstance(result, dict):
        raise ValueError("alpaca-py returned raw data")
    return result


def _json_object(reply: object) -> RawData:
    """A JSON reply that must be an object, such as one order."""
    if not isinstance(reply, dict):
        raise ValueError(f"expected a JSON object from Alpaca, got {type(reply).__name__}")
    return reply


def _decimal(value: str | float | None) -> Decimal:
    """An SDK number as a Decimal; floats go through `str` so 100.1 stays 100.1."""
    if value is None:
        raise ValueError("Alpaca left out a required number")
    return Decimal(str(value))


def _price(price: Decimal) -> str:
    """A price as Alpaca accepts it: rounded to the tick (`round_price`), as a string."""
    return f"{round_price(price):f}"


def _order_id(order_id: str) -> str:
    """`order_id` if it has Alpaca's UUID form; BrokerError otherwise, before any request."""
    try:
        return str(uuid.UUID(order_id))
    except ValueError as exc:
        raise BrokerError(f"not an Alpaca order id: {order_id!r}") from exc


def _start_of_market_day(moment: datetime) -> datetime:
    """Midnight in New York on `moment`'s New York date; Alpaca stamps daily bars with it."""
    return datetime.combine(market_date(moment), time(), tzinfo=MARKET_TZ)


def _flatten(sdk_order: SdkOrder) -> Iterator[Order]:
    """A nested Alpaca order, then each of its legs with `parent_id` set."""
    entry = _order(sdk_order)
    yield entry
    for leg in sdk_order.legs or []:
        yield _order(leg, parent_id=entry.id)


def _order(sdk_order: SdkOrder, parent_id: str | None = None) -> Order:
    order_type = sdk_order.type or sdk_order.order_type
    status = sdk_order.status.value
    filled_qty = int(_decimal(sdk_order.filled_qty or 0))
    return Order.model_validate(
        {
            "id": str(sdk_order.id),
            "client_order_id": sdk_order.client_order_id,
            "symbol": sdk_order.symbol,
            "side": sdk_order.side.value if sdk_order.side else None,
            "type": order_type.value if order_type else None,
            "order_class": sdk_order.order_class.value,
            "status": _ORDER_STATUS_ALIASES.get(status, status),
            "qty": int(_decimal(sdk_order.qty)),
            "filled_qty": filled_qty,
            "filled_avg_price": _optional_decimal(sdk_order.filled_avg_price)
            if filled_qty
            else None,
            "limit_price": _optional_decimal(sdk_order.limit_price),
            "stop_price": _optional_decimal(sdk_order.stop_price),
            "time_in_force": sdk_order.time_in_force.value,
            "parent_id": parent_id,
            "created_at": sdk_order.created_at,
        }
    )


def _optional_decimal(value: str | float | None) -> Decimal | None:
    return None if value is None else _decimal(value)


def _bar(bar: SdkBar) -> Bar:
    return Bar(
        timestamp=bar.timestamp,
        open=_decimal(bar.open),
        high=_decimal(bar.high),
        low=_decimal(bar.low),
        close=_decimal(bar.close),
        volume=int(bar.volume),
    )


def _mover(mover: SdkMover) -> Mover:
    return Mover(
        symbol=mover.symbol,
        price=_decimal(mover.price),
        change=_decimal(mover.change),
        # Alpaca reports the change in percent (5.0 for up 5%).
        change_pct=mover.percent_change / 100,
    )


def _fill_from_row(index: int, row: object) -> Fill:
    """One `FILL` activity row as a `Fill`.

    Alpaca sometimes leaves `account_id` out of a row, which `TradeActivity` demands but
    nothing here uses, so only that field is filled in. Any other missing or malformed
    field raises, naming the row and field but not the values: a dropped sell fill would
    give settlement a wrong exit price.
    """
    fields = dict(_json_object(row))
    if fields.get("account_id") is None:
        fields["account_id"] = _NO_ACCOUNT_ID
    try:
        return _fill(TradeActivity(**fields))
    except ValidationError as exc:
        problems = ", ".join(
            f"{'.'.join(str(part) for part in err['loc'])} ({err['type']})"
            for err in exc.errors(include_input=False)
        )
        raise ValueError(f"fill row {index} is unusable: {problems}") from None


def _fill(activity: TradeActivity) -> Fill:
    return Fill(
        id=activity.id,
        order_id=str(activity.order_id),
        symbol=activity.symbol,
        side=activity.side.value,
        qty=int(_decimal(activity.qty)),
        price=_decimal(activity.price),
        filled_at=activity.transaction_time,
    )


if TYPE_CHECKING:
    # Static check that AlpacaBroker satisfies the Broker Protocol.
    _: Broker = AlpacaBroker.from_settings(Settings())
