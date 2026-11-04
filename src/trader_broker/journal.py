"""The trade journal: why each buy was placed, and later how it turned out.

Every placed buy gets an open journal entry holding the proposer's reasons and the
order's final prices, linked to the review that cleared it. Once the symbol is no
longer held the entry is closed at the average price of its sell fills, and once
SPY's daily close for the exit date is known it is settled against SPY over the
same holding window and its lesson is marked pending. An entry whose buy expired
unfilled is closed with no exit price and never settled.

An entry records the order as placed: its limit and order qty. When it closes, they
are replaced by the bracket entry's fill (average price and filled qty) whenever the
broker reports one, so returns are measured from what was actually paid.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from trader_broker.brokers.base import Broker
from trader_broker.market import BENCHMARK_SYMBOL, close_on_or_before, market_date, simple_return
from trader_broker.models import (
    Bar,
    Fill,
    JournalEntry,
    Order,
    Position,
    Proposal,
    SizedOrder,
)
from trader_broker.state import StateStore

_EXIT_PRICE_TICK = Decimal("0.0001")

logger = logging.getLogger(__name__)


def record_entry(
    state: StateStore,
    proposal: Proposal,
    order: SizedOrder,
    client_order_id: str,
    review_id: int | None,
    opened_at: datetime,
) -> JournalEntry:
    """Journal a placed bracket buy and return the new open entry.

    `proposal` supplies the thesis, invalidation and horizon; `order` is the final
    order as submitted, whose entry, stop, target and qty are recorded (a reviewer
    may have raised the stop or shrunk the size). `client_order_id` is the bracket
    entry's, and `review_id` the stored review that cleared the buy. Raises
    `StateUnavailableError` when the entry cannot be written.
    """
    return state.add_journal_entry(
        symbol=order.symbol,
        client_order_id=client_order_id,
        opened_at=opened_at,
        qty=order.qty,
        entry_price=order.entry_price,
        stop_price=order.stop_price,
        target_price=order.target_price,
        thesis=proposal.thesis,
        invalidation=proposal.invalidation,
        horizon_days=proposal.horizon_days,
        review_id=review_id,
    )


def settle_trades(
    state: StateStore,
    broker: Broker,
    positions: Sequence[Position],
    orders: Sequence[Order],
    now: datetime,
) -> None:
    """Close the open entries whose symbol is no longer held, then settle closed ones.

    `positions` are the broker's current positions and `orders` its recent orders;
    an entry order missing from `orders` is looked up by client order id. An open
    entry whose symbol is absent from `positions` is closed at the qty-weighted
    average price of the symbol's sell fills since the entry opened, with `closed_at`
    the last such fill, and its entry price and qty become the bracket entry's average
    fill price and filled qty when the order reports them. It stays open while its buy
    order is open and unfilled, or
    while the broker reports no such sell. A buy order that ended without filling
    closes its entry at `now` with no exit price; it is never settled. A partly sold
    position is still held, so its entries stay open.

    A closed entry with an exit price is settled once SPY's daily close for its exit
    date is available (daily bars end at the previous close, so a trade closed today
    settles on a later trading day): `spy_return_pct` runs from SPY's close on the
    entry date to its close on the exit date, `alpha_pct` is the trade's return less
    SPY's, and the lesson is marked pending. Raises `BrokerError` when the broker
    cannot be read and `StateUnavailableError` when state cannot be read or written;
    entries handled before the error keep their changes.
    """
    held = {position.symbol for position in positions if position.qty > 0}
    leaving = [
        entry for entry in state.journal_entries("open", limit=None) if entry.symbol not in held
    ]
    if leaving:
        orders_by_client_id = {order.client_order_id: order for order in orders}
        fills = broker.get_fills(min(entry.opened_at for entry in leaving))
        for entry in leaving:
            buy = orders_by_client_id.get(entry.client_order_id)
            if buy is None:
                buy = broker.get_order_by_client_id(entry.client_order_id)
            _close(state, entry, buy, fills, now)

    today = market_date(now)
    unsettled = [
        entry
        for entry in state.journal_entries("closed", limit=None)
        if entry.spy_return_pct is None
        and entry.exit_price is not None
        and entry.closed_at is not None
        and market_date(entry.closed_at) < today
    ]
    if unsettled:
        first_entry_date = min(market_date(entry.opened_at) for entry in unsettled)
        # A session per calendar day is more than enough to reach back to that date.
        bars = broker.get_daily_bars(BENCHMARK_SYMBOL, (today - first_entry_date).days + 1)
        for entry in unsettled:
            _settle(state, entry, bars)


def _close(
    state: StateStore,
    entry: JournalEntry,
    buy: Order | None,
    fills: Sequence[Fill],
    now: datetime,
) -> None:
    """Close `entry`, whose symbol is not held, if its buy ended unfilled or was sold."""
    if buy is not None and buy.filled_qty == 0:
        if not buy.is_open:
            state.close_journal_entry(entry.id, now, exit_price=None, trade_return_pct=None)
            logger.info(
                "closed journal entry %d for %s: its buy ended %s without a fill",
                entry.id,
                entry.symbol,
                buy.status,
            )
        return
    sells = [
        fill
        for fill in fills
        if fill.symbol == entry.symbol and fill.side == "sell" and fill.filled_at >= entry.opened_at
    ]
    if not sells:
        return
    sold_qty = sum(fill.qty for fill in sells)
    proceeds = sum((fill.price * fill.qty for fill in sells), Decimal(0))
    exit_price = (proceeds / sold_qty).quantize(_EXIT_PRICE_TICK)
    entry_price, qty = entry.entry_price, entry.qty
    if buy is not None and buy.filled_qty and buy.filled_avg_price is not None:
        entry_price, qty = buy.filled_avg_price.quantize(_EXIT_PRICE_TICK), buy.filled_qty
    state.close_journal_entry(
        entry.id,
        max(fill.filled_at for fill in sells),
        exit_price,
        float(simple_return(entry_price, exit_price)),
        entry_price=entry_price,
        qty=qty,
    )
    logger.info(
        "closed journal entry %d for %s at %s (entry %s)",
        entry.id,
        entry.symbol,
        exit_price,
        entry_price,
    )


def _settle(state: StateStore, entry: JournalEntry, spy_bars: Sequence[Bar]) -> None:
    """Settle closed `entry` against SPY when `spy_bars` hold both window closes."""
    assert entry.exit_price is not None and entry.closed_at is not None
    entry_date, exit_date = market_date(entry.opened_at), market_date(entry.closed_at)
    if not spy_bars or market_date(spy_bars[-1].timestamp) < exit_date:
        return  # SPY's close for the exit date is not published yet
    start_close = close_on_or_before(spy_bars, entry_date)
    end_close = close_on_or_before(spy_bars, exit_date)
    if start_close is None or end_close is None:
        logger.warning(
            "cannot settle journal entry %d: no %s close on or before %s",
            entry.id,
            BENCHMARK_SYMBOL,
            entry_date,
        )
        return
    trade_return = simple_return(entry.entry_price, entry.exit_price)
    spy_return = simple_return(start_close, end_close)
    state.settle_journal_entry(entry.id, float(spy_return), float(trade_return - spy_return))
