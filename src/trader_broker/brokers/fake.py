"""In-memory `Broker` for tests: deterministic, scriptable, and strict where Alpaca is."""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from trader_broker.brokers.base import Broker, BrokerError, UnknownResultError
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
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    TradingDay,
)

_BROKER_METHODS = frozenset(name for name in dir(Broker) if not name.startswith("_"))
_WRITE_METHODS = frozenset(
    {"submit_bracket_buy", "submit_sell", "submit_stop", "replace_stop", "cancel_order"}
)
_TICK = Decimal("0.01")
_DEFAULT_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


@dataclass
class _Holding:
    qty: int
    avg_entry_price: Decimal


class FakeBroker:
    """A brokerage account held in memory.

    Tests set the market and account with the `set_*` methods and move it forward
    with `set_now`, `set_price`, `fill_order` and `trigger_stop`; nothing happens on
    its own except that a market sell fills at once at the bid. Unset lists read as
    empty; an unset quote or clock raises `BrokerError`, as an unknown symbol would
    at a real broker.

    Behaves like Alpaca where the server depends on it:

    - A bracket buy is three orders: the limit entry, a take-profit limit leg
      (`held` until the entry fills, then `new`) and a stop leg (`held`). The legs
      are inactive until the entry has filled completely, or was cancelled after a
      partial fill: until then they reserve and can fill nothing. Active legs cover
      the entry's filled qty; when one leg fills, the other is cancelled.
    - A sell (market or stop) is rejected when its qty exceeds the position's qty
      minus the qty reserved by open sell orders, active bracket legs included.
    - Client order ids are unique; `replace_stop` returns a new order with a new id
      and the client order id it is given, and marks the old one `replaced`.
    - Cancelling an unfilled bracket entry cancels its legs; cancelling a partly
      filled one activates the legs for the filled shares.
    - Cancels take effect at once, unless `delay_cancels` makes them asynchronous as
      Alpaca's are: the order then reads `pending_cancel` for a number of reads.

    Scripted failures (`fail_next`, `accept_then_time_out`) apply to the next call
    of the named method only.
    """

    def __init__(self, now: datetime = _DEFAULT_NOW) -> None:
        self._now = now
        self._ids = itertools.count(1)
        self._cash = Decimal(0)
        self._last_equity = Decimal(0)
        self._holdings: dict[str, _Holding] = {}
        self._quotes: dict[str, Quote] = {}
        self._assets: dict[str, Asset] = {}
        self._daily_bars: dict[str, list[Bar]] = {}
        self._bars: dict[tuple[str, str], list[Bar]] = {}
        self._news: list[NewsItem] = []
        self._movers = Movers(gainers=[], losers=[])
        self._clock: Clock | None = None
        self._calendar: list[TradingDay] = []
        self._portfolio_history: list[EquityPoint] = []
        self._fills: list[Fill] = []
        self._orders: dict[str, Order] = {}
        """Every order by id, in creation order."""
        self._next_errors: dict[str, BrokerError] = {}
        self._next_timeouts: set[str] = set()
        self._cancel_delay_reads = 0
        self._pending_cancels: dict[str, int] = {}
        """Order id -> `get_orders` reads left before its requested cancel takes effect."""

    # ----- Test setup -----

    def set_now(self, now: datetime) -> None:
        """Set the time stamped on new orders and fills."""
        self._now = now

    def set_account(self, cash: Decimal, last_equity: Decimal | None = None) -> None:
        """Set cash and the previous close's equity (default: equity as it now stands).

        Equity is always cash plus the market value of the positions.
        """
        self._cash = cash
        self._last_equity = self._equity() if last_equity is None else last_equity

    def set_position(
        self,
        symbol: str,
        qty: int,
        avg_entry_price: Decimal,
        current_price: Decimal | None = None,
    ) -> None:
        """Hold `qty` shares (0 removes the position), optionally moving the price too."""
        if qty:
            self._holdings[symbol] = _Holding(qty, avg_entry_price)
        else:
            self._holdings.pop(symbol, None)
        if current_price is not None:
            self.set_price(symbol, current_price)

    def set_quote(self, symbol: str, bid: Decimal, ask: Decimal) -> None:
        """Set the symbol's quote; positions are valued at its midpoint."""
        self._quotes[symbol] = Quote(
            symbol=symbol, bid_price=bid, ask_price=ask, timestamp=self._now
        )

    def set_price(self, symbol: str, price: Decimal) -> None:
        """Move the market: bid and ask both at `price`."""
        self.set_quote(symbol, price, price)

    def set_asset(self, asset: Asset) -> None:
        self._assets[asset.symbol] = asset

    def set_daily_bars(self, symbol: str, bars: list[Bar]) -> None:
        """Daily bars, oldest first, ending at the previous close."""
        self._daily_bars[symbol] = list(bars)

    def set_bars(self, symbol: str, timeframe: str, bars: list[Bar]) -> None:
        """Bars of one timeframe, oldest first."""
        self._bars[symbol, timeframe] = list(bars)

    def set_news(self, news: list[NewsItem]) -> None:
        self._news = list(news)

    def set_movers(self, movers: Movers) -> None:
        self._movers = movers

    def set_clock(self, clock: Clock) -> None:
        self._clock = clock

    def set_calendar(self, days: list[TradingDay]) -> None:
        self._calendar = list(days)

    def set_portfolio_history(self, points: list[EquityPoint]) -> None:
        self._portfolio_history = list(points)

    def add_fill(self, fill: Fill) -> None:
        """Record a fill without touching orders, positions or cash."""
        self._fills.append(fill)

    def fail_next(self, method: str, error: BrokerError | None = None) -> None:
        """Make the next call to `method` raise `error` (default: a `BrokerError`)
        before it does anything. Pass a `RateLimitError` to script a rate limit."""
        if method not in _BROKER_METHODS:
            raise ValueError(f"not a Broker method: {method}")
        self._next_errors[method] = error or BrokerError(f"scripted failure in {method}")

    def accept_then_time_out(self, method: str) -> None:
        """Make the next call to write `method` take effect, then raise `UnknownResultError`."""
        if method not in _WRITE_METHODS:
            raise ValueError(f"not a Broker write method: {method}")
        self._next_timeouts.add(method)

    def delay_cancels(self, reads: int) -> None:
        """Make every later cancel asynchronous: the order reads `pending_cancel` for the
        next `reads` calls of `get_orders` (still open, still reserving shares), and is
        cancelled, with the effects on its legs, just before the read after those.
        0 makes cancels immediate again."""
        self._cancel_delay_reads = reads

    def fill_order(
        self, order_id: str, qty: int | None = None, price: Decimal | None = None
    ) -> Fill:
        """Fill `qty` shares of an open order (default: all it can still fill) at `price`
        (default: its limit or stop price, or the quote for a market order).

        Updates the position and cash. Filling a bracket entry activates its legs;
        filling a leg cancels its sibling. Raises ValueError for an order that cannot
        fill that much.
        """
        order = self._orders.get(order_id)
        if order is None or not order.is_open:
            raise ValueError(f"order {order_id} is not open")
        fillable = self._unfilled_qty(order)
        qty = fillable if qty is None else qty
        if not 0 < qty <= fillable:
            raise ValueError(f"order {order_id} can fill 1..{fillable} shares, not {qty}")
        price = price or order.limit_price or order.stop_price or self._market_price(order)
        if order.side == "sell" and qty > self._held_qty(order.symbol):
            raise ValueError(f"cannot sell {qty} {order.symbol}: position too small")

        self._apply_fill(order.symbol, order.side, qty, price)
        filled_qty = order.filled_qty + qty
        filled_cost = (order.filled_avg_price or Decimal(0)) * order.filled_qty + price * qty
        order = order.model_copy(
            update={"filled_qty": filled_qty, "filled_avg_price": filled_cost / filled_qty}
        )
        self._orders[order_id] = order
        status: OrderStatus = "filled" if self._unfilled_qty(order) == 0 else "partially_filled"
        self._set_status(order_id, status)

        if order.order_class == "bracket" and order.parent_id is None:
            if status == "filled":
                self._activate_legs(order_id)
        elif order.parent_id is not None and status == "filled":
            for sibling in self._legs(order.parent_id):
                if sibling.id != order_id and sibling.is_open:
                    self._set_status(sibling.id, "canceled")

        fill = Fill(
            id=self._new_id("fill"),
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            filled_at=self._now,
        )
        self._fills.append(fill)
        return fill

    def trigger_stop(self, symbol: str, price: Decimal | None = None) -> list[Fill]:
        """Fill every open stop sell for `symbol` that covers shares.

        With `price`, the market moves there first and the stops fill at it (a gap);
        without, each stop fills at its stop price. Raises ValueError if none is open.
        """
        stops = [
            order
            for order in self._orders.values()
            if order.symbol == symbol
            and order.type == "stop"
            and order.side == "sell"
            and order.is_open
            and self._unfilled_qty(order) > 0
        ]
        if not stops:
            raise ValueError(f"no open stop covers {symbol}")
        if price is not None:
            self.set_price(symbol, price)
        return [self.fill_order(stop.id, price=price) for stop in stops]

    # ----- Broker: reads -----

    def get_account(self) -> Account:
        self._raise_scripted_error("get_account")
        return Account(
            equity=self._equity(),
            last_equity=self._last_equity,
            cash=self._cash,
            buying_power=max(self._cash, Decimal(0)),
        )

    def get_positions(self) -> list[Position]:
        self._raise_scripted_error("get_positions")
        return self._positions()

    def get_orders(
        self, status: Literal["open", "closed", "all"] = "open", limit: int = 100
    ) -> list[Order]:
        self._raise_scripted_error("get_orders")
        self._advance_pending_cancels()
        orders = [
            order
            for order in reversed(self._orders.values())
            if status == "all" or order.is_open == (status == "open")
        ]
        return orders[:limit]

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        self._raise_scripted_error("get_order_by_client_id")
        return self._find_by_client_id(client_order_id)

    def get_asset(self, symbol: str) -> Asset | None:
        self._raise_scripted_error("get_asset")
        return self._assets.get(symbol)

    def get_quote(self, symbol: str) -> Quote:
        self._raise_scripted_error("get_quote")
        if symbol not in self._quotes:
            raise BrokerError(f"no quote for {symbol}")
        return self._quotes[symbol]

    def get_daily_bars(self, symbol: str, days: int, *, sip_history: bool = True) -> list[Bar]:
        self._raise_scripted_error("get_daily_bars")
        return _latest(self._daily_bars.get(symbol, []), days)

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        self._raise_scripted_error("get_bars")
        return _latest(self._bars.get((symbol, timeframe), []), limit)

    def get_news(self, symbols: list[str] | None, limit: int) -> list[NewsItem]:
        self._raise_scripted_error("get_news")
        matching = [
            item
            for item in self._news
            if symbols is None or set(symbols).intersection(item.symbols)
        ]
        matching.sort(key=lambda item: item.created_at, reverse=True)
        return matching[:limit]

    def get_movers(self, limit: int) -> Movers:
        self._raise_scripted_error("get_movers")
        return Movers(gainers=self._movers.gainers[:limit], losers=self._movers.losers[:limit])

    def get_clock(self) -> Clock:
        self._raise_scripted_error("get_clock")
        if self._clock is None:
            raise BrokerError("clock not set")
        return self._clock

    def get_calendar(self, start: date, end: date) -> list[TradingDay]:
        self._raise_scripted_error("get_calendar")
        return [day for day in self._calendar if start <= day.date <= end]

    def get_portfolio_history(self, start: date) -> list[EquityPoint]:
        self._raise_scripted_error("get_portfolio_history")
        return [point for point in self._portfolio_history if point.timestamp.date() >= start]

    def get_fills(self, since: datetime) -> list[Fill]:
        self._raise_scripted_error("get_fills")
        return [fill for fill in self._fills if fill.filled_at >= since]

    # ----- Broker: writes -----

    def submit_bracket_buy(self, req: BracketOrderRequest) -> Order:
        self._raise_scripted_error("submit_bracket_buy")
        self._check_new_order(req.client_order_id, req.qty)
        if req.stop_price > req.limit_price - _TICK:
            raise BrokerError("stop_loss.stop_price must be <= base_price - 0.01")
        if req.take_profit_price < req.limit_price + _TICK:
            raise BrokerError("take_profit.limit_price must be >= base_price + 0.01")

        entry = self._add_order(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side="buy",
            type="limit",
            order_class="bracket",
            status="new",
            qty=req.qty,
            limit_price=req.limit_price,
            time_in_force=req.time_in_force,
        )
        for leg_type, price_field, price in (
            ("limit", "limit_price", req.take_profit_price),
            ("stop", "stop_price", req.stop_price),
        ):
            self._add_order(
                client_order_id=self._new_id("fake-client"),
                symbol=req.symbol,
                side="sell",
                type=leg_type,
                order_class="bracket",
                status="held",
                qty=req.qty,
                time_in_force=req.time_in_force,
                parent_id=entry.id,
                **{price_field: price},
            )
        self._raise_scripted_timeout("submit_bracket_buy")
        return entry

    def submit_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        self._raise_scripted_error("submit_sell")
        self._check_new_order(client_order_id, qty)
        self._check_sellable(symbol, qty)
        if symbol not in self._quotes:
            raise BrokerError(f"no quote for {symbol}")
        order = self._add_order(
            client_order_id=client_order_id,
            symbol=symbol,
            side="sell",
            type="market",
            order_class="simple",
            status="new",
            qty=qty,
            time_in_force="day",
        )
        self.fill_order(order.id)
        self._raise_scripted_timeout("submit_sell")
        return self._orders[order.id]

    def submit_stop(
        self, symbol: str, qty: int, stop_price: Decimal, client_order_id: str
    ) -> Order:
        self._raise_scripted_error("submit_stop")
        self._check_new_order(client_order_id, qty)
        self._check_sellable(symbol, qty)
        order = self._add_order(
            client_order_id=client_order_id,
            symbol=symbol,
            side="sell",
            type="stop",
            order_class="simple",
            status="new",
            qty=qty,
            stop_price=stop_price,
            time_in_force="gtc",
        )
        self._raise_scripted_timeout("submit_stop")
        return order

    def replace_stop(self, order_id: str, stop_price: Decimal, client_order_id: str) -> Order:
        self._raise_scripted_error("replace_stop")
        old = self._orders.get(order_id)
        if old is None or old.type != "stop" or not old.is_open:
            raise BrokerError(f"order {order_id} is not an open stop order")
        if self._find_by_client_id(client_order_id) is not None:
            raise BrokerError(f"client_order_id must be unique: {client_order_id}")
        self._set_status(order_id, "replaced")
        new = old.model_copy(
            update={
                "id": self._new_id("order"),
                "client_order_id": client_order_id,
                "stop_price": stop_price,
                "created_at": self._now,
            }
        )
        self._orders[new.id] = new
        self._raise_scripted_timeout("replace_stop")
        return new

    def cancel_order(self, order_id: str) -> None:
        self._raise_scripted_error("cancel_order")
        order = self._orders.get(order_id)
        if order is None:
            raise BrokerError(f"order {order_id} not found")
        if not order.is_open or order.status == "pending_cancel":
            raise BrokerError(f"order {order_id} is not cancelable (status {order.status})")
        if self._cancel_delay_reads:
            self._set_status(order_id, "pending_cancel")
            self._pending_cancels[order_id] = self._cancel_delay_reads
        else:
            self._finish_cancel(order_id)
        self._raise_scripted_timeout("cancel_order")

    # ----- Internals -----

    def _finish_cancel(self, order_id: str) -> None:
        """Cancel the order; an entry's legs are cancelled with it when it bought
        nothing, and activated for the shares bought otherwise."""
        order = self._orders[order_id]
        self._set_status(order_id, "canceled")
        if order.parent_id is not None or order.order_class != "bracket":
            return
        if order.filled_qty == 0:
            for leg in self._legs(order_id):
                if leg.is_open:
                    self._set_status(leg.id, "canceled")
        else:
            self._activate_legs(order_id)

    def _advance_pending_cancels(self) -> None:
        for order_id, reads_left in list(self._pending_cancels.items()):
            if reads_left > 0:
                self._pending_cancels[order_id] = reads_left - 1
                continue
            del self._pending_cancels[order_id]
            if self._orders[order_id].status == "pending_cancel":
                self._finish_cancel(order_id)

    def _activate_legs(self, entry_id: str) -> None:
        """The entry is done buying: its take-profit leg starts working (`new`)."""
        for leg in self._legs(entry_id):
            if leg.type == "limit" and leg.status == "held":
                self._set_status(leg.id, "new")

    def _raise_scripted_error(self, method: str) -> None:
        error = self._next_errors.pop(method, None)
        if error is not None:
            raise error

    def _raise_scripted_timeout(self, method: str) -> None:
        if method in self._next_timeouts:
            self._next_timeouts.discard(method)
            raise UnknownResultError(f"{method} timed out after the broker accepted it")

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def _add_order(self, **fields: object) -> Order:
        order = Order.model_validate(
            {"id": self._new_id("order"), "created_at": self._now, **fields}
        )
        self._orders[order.id] = order
        return order

    def _set_status(self, order_id: str, status: OrderStatus) -> None:
        self._orders[order_id] = self._orders[order_id].model_copy(update={"status": status})

    def _find_by_client_id(self, client_order_id: str) -> Order | None:
        return next(
            (o for o in self._orders.values() if o.client_order_id == client_order_id), None
        )

    def _legs(self, entry_id: str) -> list[Order]:
        return [order for order in self._orders.values() if order.parent_id == entry_id]

    def _unfilled_qty(self, order: Order) -> int:
        """Shares the order can still fill; a leg can fill only what its entry bought,
        and nothing while the entry is open and not completely filled."""
        if order.parent_id is None:
            return order.qty - order.filled_qty
        entry = self._orders[order.parent_id]
        if entry.is_open and entry.filled_qty < entry.qty:
            return 0
        return entry.filled_qty - sum(leg.filled_qty for leg in self._legs(entry.id))

    def _reserved_qty(self, symbol: str) -> int:
        """Shares held back by open sell orders; a bracket's two legs reserve once."""
        reserved = 0
        counted_brackets: set[str] = set()
        for order in self._orders.values():
            if order.symbol != symbol or order.side != "sell" or not order.is_open:
                continue
            if order.parent_id in counted_brackets:
                continue
            if order.parent_id is not None:
                counted_brackets.add(order.parent_id)
            reserved += self._unfilled_qty(order)
        return reserved

    def _held_qty(self, symbol: str) -> int:
        holding = self._holdings.get(symbol)
        return holding.qty if holding else 0

    def _check_new_order(self, client_order_id: str, qty: int) -> None:
        if qty < 1:
            raise BrokerError(f"qty must be at least 1, got {qty}")
        if self._find_by_client_id(client_order_id) is not None:
            raise BrokerError(f"client_order_id must be unique: {client_order_id}")

    def _check_sellable(self, symbol: str, qty: int) -> None:
        available = self._held_qty(symbol) - self._reserved_qty(symbol)
        if qty > available:
            raise BrokerError(
                f"insufficient qty available for order (requested: {qty}, available: {available})"
            )

    def _market_price(self, order: Order) -> Decimal:
        quote = self._quotes.get(order.symbol)
        if quote is None:
            raise ValueError(f"no quote to fill market order {order.id} against")
        return quote.ask_price if order.side == "buy" else quote.bid_price

    def _apply_fill(self, symbol: str, side: OrderSide, qty: int, price: Decimal) -> None:
        held = self._holdings.get(symbol, _Holding(0, Decimal(0)))
        if side == "buy":
            cost = held.avg_entry_price * held.qty + price * qty
            self._holdings[symbol] = _Holding(held.qty + qty, cost / (held.qty + qty))
            self._cash -= price * qty
        else:
            self.set_position(symbol, held.qty - qty, held.avg_entry_price)
            self._cash += price * qty

    def _equity(self) -> Decimal:
        return self._cash + sum((p.market_value for p in self._positions()), Decimal(0))

    def _positions(self) -> list[Position]:
        positions = []
        for symbol, holding in self._holdings.items():
            quote = self._quotes.get(symbol)
            price = (quote.bid_price + quote.ask_price) / 2 if quote else holding.avg_entry_price
            positions.append(
                Position(
                    symbol=symbol,
                    qty=holding.qty,
                    avg_entry_price=holding.avg_entry_price,
                    current_price=price,
                    market_value=price * holding.qty,
                )
            )
        return positions


def _latest[T](items: list[T], count: int) -> list[T]:
    """The last `count` items, oldest first."""
    return items[-count:] if count > 0 else []


if TYPE_CHECKING:
    # Static check that FakeBroker satisfies the Broker Protocol.
    _: Broker = FakeBroker()
