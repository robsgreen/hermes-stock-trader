"""The trader service: the logic behind every MCP tool, over a broker, state and reviewers.

Every tool call starts with an equity refresh that moves the halt latches. Every write
tool and `get_risk_status` then run housekeeping, so the account repairs itself after a
crash or a manual trade: stale entry orders are cancelled, unprotected positions get a
stop, and the state's orders are reconciled with the broker's.

`propose_buy`, the only way to open a position, runs housekeeping, the risk gate, the
cross-model review and a re-check of the final order before it submits one bracket.

Housekeeping also closes journal entries whose symbol is no longer held and settles
them against SPY; `get_journal` settles the same way before it reads, and
`journal_note` records lessons.

A buy whose submit reply is lost and whose order the broker cannot show yet is left
unresolved, never resubmitted: housekeeping later asks the broker for it by client
order id, journals it if it was placed, and closes it if it was not. Until then new
buys of its symbol are blocked.

The service keeps working when the state database is unavailable: it skips every state
read and write and still cancels stale entries and protects positions. Only buys and
the journal tools need state; buys are blocked without it.
"""

import functools
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from trader_broker.brokers.base import Broker, BrokerError, UnknownResultError
from trader_broker.config import Settings
from trader_broker.journal import record_entry, settle_trades
from trader_broker.market import BENCHMARK_SYMBOL, market_date
from trader_broker.models import (
    Account,
    Asset,
    Bar,
    BlockRule,
    BracketOrderRequest,
    JournalEntry,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    Proposal,
    Quote,
    ReviewOutcome,
    SizedOrder,
    VerdictName,
    round_price,
)
from trader_broker.review import (
    MAX_HEADLINES,
    Reviewer,
    ReviewPacket,
    build_packet,
    decide,
    run_reviewers,
)
from trader_broker.risk import (
    Caps,
    HaltCheck,
    Portfolio,
    check_entry,
    check_halts,
    check_market_rules,
    entry_price,
    size_position,
)
from trader_broker.state import (
    Block,
    HaltState,
    JournalStatus,
    OrderRole,
    StateStore,
    StateUnavailableError,
    StoredOrder,
)

CLIENT_ORDER_ID_PREFIX = "tb-"
"""Start of the client order id of every order trader-broker places."""

DEFAULT_STOP_PCT = Decimal("0.08")
"""How far below the average entry price housekeeping puts a stop when the journal
records none, as a share of that price."""

_ORDER_HISTORY_LIMIT = 500
"""Most recent orders housekeeping reads (Alpaca's maximum page size)."""

_DAILY_BAR_COUNT = 260
"""Daily bars a buy reads for its symbol and for SPY: a year of sessions, enough for
the 200-day average and the 52-week high in the review packet."""

_UNKNOWN_ENTRY_GRACE = timedelta(minutes=5)
"""How long after its submit an entry the broker does not have may still appear; past
it, housekeeping closes the entry's unresolved intent as never placed."""

_STOP_TYPES = frozenset({"stop", "stop_limit", "trailing_stop"})
"""Order types that sell once the price falls to a stop: every one of them protects
shares. `tighten_stop` can move only the plain `stop`, the only type trader-broker
places."""

_CANCEL_CONFIRM_ATTEMPTS = 10
_CANCEL_CONFIRM_INTERVAL_S = 0.5
"""A sell waits up to (attempts - 1) x interval for the broker to confirm its cancels;
Alpaca cancels asynchronously, through `pending_cancel`."""

_REPROTECT_WAIT_ATTEMPTS = 20
"""After unconfirmed cancels, re-protection reads the orders up to this many times,
`_CANCEL_CONFIRM_INTERVAL_S` apart, until no sell of the symbol is still being
cancelled: such an order reserves its shares, so a stop for them would be refused."""

_RETRY_PROTECTION = (
    "the next housekeeping pass places the stop once the cancels finish: "
    "call get_risk_status again in a minute"
)
"""What the agent should do about a repair refused while cancels are still pending."""

logger = logging.getLogger(__name__)


def new_client_order_id() -> str:
    """A fresh client order id that marks an order as placed by trader-broker."""
    return f"{CLIENT_ORDER_ID_PREFIX}{uuid4()}"


@dataclass(frozen=True)
class AccountReading:
    """The account as read at the start of a tool call, and the halts it leaves set."""

    account: Account
    halts: HaltCheck | None
    """Halt latches after this reading; None when state is unavailable, since the
    stored peak and latches cannot be read."""


class ProtectionRepair(BaseModel):
    """A position that had shares no working stop order covered, and how they were
    protected.

    Housekeeping reports these; a sell reports the shares it leaves this way.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    qty: int
    """Shares held."""
    uncovered_qty: int
    """Shares no working stop order covered. A stop counts while it is open and not
    being cancelled; a bracket leg only once its entry has filled completely or
    stopped buying, since the broker holds it inactive until then."""
    stop_price: Decimal
    """The stop chosen for the uncovered shares: in housekeeping, the journal's recorded
    stop, else `DEFAULT_STOP_PCT` below the average entry; after a sell, the previous
    stop."""
    action: Literal["stop_placed", "sold", "failed"]
    """`stop_placed`: a GTC stop now covers the shares. `sold`: the bid was already at
    or below the stop, so they were market-sold (never when there is no bid, or while
    cancels are unconfirmed). `failed`: the broker refused; the next housekeeping
    pass tries again."""
    client_order_id: str | None = None
    """The stop or sell order placed; None when the action failed."""
    error: str | None = None
    """Why the broker refused, when the action failed."""


@dataclass(frozen=True)
class HousekeepingResult:
    """What one housekeeping pass saw and did."""

    reading: AccountReading
    positions: list[Position]
    """Positions as read before any repair sell."""
    orders: list[Order]
    """Recent orders, open and closed, as read after stale entries were cancelled and
    before repairs were placed."""
    protection_repairs: list[ProtectionRepair]


ExitReason = Literal[
    "invalid_qty",
    "exceeds_position",
    "no_position",
    "no_stop",
    "stop_not_raised",
    "stop_at_or_above_bid",
    "unsupported_stop",
    "not_cancellable",
    "cancel_unconfirmed",
    "broker_error",
]
"""Why an exit tool rejected a request or could not finish it."""


class ExitResult(BaseModel):
    """What `sell`, `close_position`, `tighten_stop` or `cancel_entry_order` did."""

    model_config = ConfigDict(frozen=True)

    status: Literal["done", "rejected", "failed"]
    """`done`: carried out. `rejected`: the request was invalid and nothing was sent for
    it. `failed`: the broker refused an order or did not confirm a cancel in time."""
    reason: ExitReason | None = None
    """Set exactly when `status` is not `done`."""
    message: str
    """What happened, in plain language, with the numbers involved."""
    orders: list[Order] = Field(default_factory=list)
    """Orders this call placed or replaced: a sell, or the raised stops."""
    protection_repairs: list[ProtectionRepair] = Field(default_factory=list)
    """After a partial or failed sell, or cancels the broker did not confirm: how the
    shares still held that no working stop covered were protected."""


class RiskStatus(BaseModel):
    """The account's risk picture, returned by `get_risk_status`.

    Every `_pct` field is a share of a whole (0.05 for 5%). Fields that need stored
    state (peak, drawdown, halts, recent blocks) are None or empty when state is
    unavailable.
    """

    model_config = ConfigDict(frozen=True)

    mode: Literal["paper", "live"]
    state_available: bool
    equity: Decimal
    last_equity: Decimal
    """Equity at the previous close."""
    day_pnl_pct: float
    """Change in equity since the previous close, as a share of that close's equity."""
    peak: Decimal | None
    drawdown_pct: float | None
    """How far equity is below the peak, as a share of the peak."""
    kill_switch: bool | None
    daily_halt: bool | None
    """True while today's daily halt blocks new buys."""
    halt_reasons: list[str]
    exposure_pct: float
    """Market value of all positions as a share of equity."""
    cash: Decimal
    symbol_pct: dict[str, float]
    """Each held symbol's market value as a share of equity."""
    protection_repairs: list[ProtectionRepair]
    """Positions this call's housekeeping found with shares no working stop covered,
    and how it protected them."""
    open_entry_orders: list[Order]
    recent_blocks: list[Block]

    def as_text(self) -> str:
        """The status for a person to read, holding the same values as the JSON form."""

        def pct(share: float | None) -> str:
            return "unknown" if share is None else f"{share:.2%}"

        def flag(latched: bool | None) -> str:
            return {None: "unknown", True: "SET", False: "clear"}[latched]

        lines = [
            f"Mode: {self.mode}"
            + ("" if self.state_available else " (state database unavailable: buys blocked)"),
            f"Equity: ${self.equity:,.2f}  day {self.day_pnl_pct:+.2%}  cash ${self.cash:,.2f}",
            f"Peak: {'unknown' if self.peak is None else f'${self.peak:,.2f}'}"
            f"  drawdown {pct(self.drawdown_pct)}",
            f"Kill switch: {flag(self.kill_switch)}  daily halt: {flag(self.daily_halt)}",
            f"Exposure: {self.exposure_pct:.2%}",
        ]
        lines += [f"  halt: {reason}" for reason in self.halt_reasons]
        lines += [f"  {symbol}: {share:.2%}" for symbol, share in self.symbol_pct.items()]

        def section(title: str, items: list[str]) -> None:
            lines.append(f"{title}:" if items else f"{title}: none")
            lines.extend(f"  {item}" for item in items)

        section(
            "Protection repairs",
            [
                f"{item.symbol}: {item.uncovered_qty} of {item.qty} shares, {item.action}"
                f" at {item.stop_price}" + (f" ({item.error})" if item.error else "")
                for item in self.protection_repairs
            ],
        )
        section(
            "Open entry orders",
            [
                f"{order.symbol}: {order.qty} at {order.limit_price}, {order.status}"
                f" ({order.client_order_id})"
                for order in self.open_entry_orders
            ],
        )
        section(
            "Recent blocks",
            [
                f"{block.at:%Y-%m-%d %H:%M} {block.tool} {block.symbol}: {'; '.join(block.reasons)}"
                for block in self.recent_blocks
            ],
        )
        return "\n".join(lines)


class ReviewSummary(BaseModel):
    """One reviewer's answer on a proposed buy, as `propose_buy` reports it."""

    model_config = ConfigDict(frozen=True)

    reviewer: str
    verdict: VerdictName
    """`error`: no valid verdict was had, even after a retry; `reasons` says why."""
    confidence: Literal["low", "medium", "high"] | None
    """None for an error."""
    reasons: list[str]


class PlacedBuy(BaseModel):
    """The bracket buy `propose_buy` placed, at its final size and prices."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    qty: int
    limit_price: Decimal
    """The entry limit."""
    stop_price: Decimal
    target_price: Decimal
    """The take-profit limit."""
    client_order_id: str
    order_id: str
    """The broker's id of the entry order."""
    status: OrderStatus
    """The entry's broker status when placed."""


class BuyResult(BaseModel):
    """What `propose_buy` did."""

    model_config = ConfigDict(frozen=True)

    status: Literal["placed", "blocked", "unknown"]
    """`unknown`: the broker's reply to the submit was lost and it could not show the
    order yet. It was not resubmitted; housekeeping finds out by its client order id,
    and new buys of the symbol are blocked (`pending_unknown`) until it does."""
    reasons: list[str]
    """When blocked, every reason, each starting with its rule id (`daily_halt: ...`,
    `broker_error: ...`) or, for a review block, the reviewer's name. When placed, the
    reviewers' changes that were applied, each naming its reviewer. When unknown,
    what happened, with the client order id."""
    order: PlacedBuy | None = None
    """Set exactly when placed."""
    reviews: list[ReviewSummary] = Field(default_factory=list)
    """Every reviewer's answer; empty when the buy was blocked before review."""


@dataclass(frozen=True)
class _GatePassed:
    """A buy the risk gate allowed, with the broker reads it was judged on."""

    sized: SizedOrder
    portfolio: Portfolio
    asset: Asset | None
    quote: Quote
    daily_bars: list[Bar]
    """The symbol's SIP daily bars, oldest first, ending at the previous close."""


class _BuyBlocked(Exception):
    """A stage of `propose_buy` refused the buy: `rules` are the ids of the rules that
    refused it, `reasons` say why, each led by its rule id or reviewer."""

    def __init__(self, rules: Sequence[BlockRule], reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.rules = list(rules)
        self.reasons = reasons


class _BuyOutcomeUnknown(Exception):
    """The bracket's submit reply was lost and the broker could not show the order."""


class TraderService:
    """The tools' logic over one broker account and its state database.

    Methods raise `BrokerError` when the broker cannot be read; an exit tool whose
    order the broker refuses returns a `failed` result instead, and `propose_buy`
    returns a `broker_error` block (or an `unknown` result when a submit reply is lost
    and the order cannot be found yet). When state is unavailable the service carries on
    without it: buys report `state_unavailable`, and only the journal tools, which
    have nothing to work on without it, raise `StateUnavailableError`.
    """

    def __init__(
        self,
        broker: Broker,
        state: StateStore | None,
        settings: Settings,
        reviewers: Sequence[Reviewer],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Serve `broker` with `state` (None: the database is unavailable).

        `now` returns the current timezone-aware time and `sleep` waits that many
        seconds between broker polls; tests inject a fixed clock and a no-op sleep.
        """
        self._broker = broker
        self._state = state
        self._settings = settings
        self._caps = Caps.from_settings(settings)
        self._reviewers = list(reviewers)
        self._now = now
        self._sleep = sleep

    def refresh_equity(self) -> AccountReading:
        """Read the account and move the halt latches; run at the start of every tool.

        Raises the stored peak to the current equity, latches the daily halt and the
        kill switch when their limits are reached, and stores the result. The read
        and write of the latches are one transaction, so a kill switch reset from the
        CLI meanwhile is never overwritten. Without state the account is still
        returned, with `halts` None.
        """
        account = self._broker.get_account()
        today = market_date(self._now())

        def check(stored: HaltState) -> HaltCheck:
            return check_halts(
                account.equity,
                account.last_equity,
                stored.peak,
                stored.kill_switch,
                stored.daily_halt_date,
                today,
                self._caps,
            )

        def latch(stored: HaltState) -> HaltState:
            halts = check(stored)
            return HaltState(halts.peak, halts.kill_switch, halts.daily_halt_date)

        if self._state is None:
            return AccountReading(account, halts=None)
        try:
            stored = self._state.update_halts(latch)
        except StateUnavailableError as exc:
            logger.warning("state unavailable; halt latches not updated: %s", exc)
            return AccountReading(account, halts=None)
        # `stored` already holds this reading, so checking it again changes nothing
        # and yields the failures that block buys now.
        return AccountReading(account, halts=check(stored))

    def read_broker[T](self, read: Callable[[Broker], T]) -> T:
        """Refresh equity, then return `read(broker)`: the read tools' path to the broker.

        Raises BrokerError when either the account or `read` fails.
        """
        self.refresh_equity()
        return read(self._broker)

    def housekeeping(self) -> HousekeepingResult:
        """Refresh equity, then repair the account; run before every write tool.

        In order: resolve entries whose submit outcome is unknown, by asking the broker
        for their client order id (a found order is journaled; one still missing
        after `_UNKNOWN_ENTRY_GRACE` is closed as never placed); cancel entry orders
        past `entry_ttl_minutes` (a partly filled one loses only its unfilled
        remainder, and its legs start protecting the filled shares); cancel
        trader-broker's own standalone stops that cover more shares than are held,
        held symbols or not (a repair stop placed while a bracket's legs were
        inactive, or left after a leg sold the whole position); give every position's
        shares that no working stop covers a GTC stop, or market-sell them when the
        bid is real and already at or below that stop, first cancelling the sell
        orders that hold those shares back without a stop (a take-profit leg whose
        stop leg is gone, a plain limit sell); reconcile the state's orders with the
        broker's and record integrity violations; close and settle the journal
        entries of symbols no longer held. A broker refusal of one cancel or repair,
        or a failure to settle the journal, is logged and the pass goes on.
        """
        reading = self.refresh_equity()
        self._resolve_unknown_entries()
        self._cancel_stale_entries(self._broker.get_orders("open"))

        # Read after the cancels, so coverage sees which legs they activated.
        orders = self._broker.get_orders("all", limit=_ORDER_HISTORY_LIMIT)
        positions = self._broker.get_positions()
        open_entries = self._open_journal_entries()
        repairs = self._protect_positions(positions, orders, open_entries)
        self._reconcile(reading.account, positions, orders, open_entries)
        self._settle_journal(positions, orders)
        return HousekeepingResult(reading, positions, orders, repairs)

    def get_risk_status(self) -> RiskStatus:
        """Run housekeeping, then report equity, halts, exposure and open risk."""
        result = self.housekeeping()
        account, halts = result.reading.account, result.reading.halts
        today = market_date(self._now())

        def share(amount: Decimal, whole: Decimal) -> float:
            return float(amount / whole) if whole > 0 else 0.0

        symbol_value = {position.symbol: position.market_value for position in result.positions}
        recent_blocks: list[Block] = self._read_state(lambda state: state.recent_blocks(), [])
        return RiskStatus(
            mode=self._settings.mode,
            state_available=halts is not None,
            equity=account.equity,
            last_equity=account.last_equity,
            day_pnl_pct=share(account.equity - account.last_equity, account.last_equity),
            peak=halts.peak if halts else None,
            drawdown_pct=share(halts.peak - account.equity, halts.peak) if halts else None,
            kill_switch=halts.kill_switch if halts else None,
            daily_halt=halts.daily_halt_date == today if halts else None,
            halt_reasons=halts.reasons if halts else [],
            exposure_pct=share(sum(symbol_value.values(), Decimal(0)), account.equity),
            cash=account.cash,
            symbol_pct={
                symbol: share(value, account.equity) for symbol, value in symbol_value.items()
            },
            protection_repairs=result.protection_repairs,
            open_entry_orders=[order for order in result.orders if _is_open_entry(order)],
            recent_blocks=recent_blocks,
        )

    # Housekeeping steps

    def _resolve_unknown_entries(self) -> None:
        """Settle each entry whose submit outcome is unknown, by its client order id.

        A found order is adopted: its broker status is stored and it is journaled from
        its recorded proposal, sizing and review. One the broker still does not have
        `_UNKNOWN_ENTRY_GRACE` after its submit is closed as `rejected`, never placed.
        A failed lookup leaves the entry unresolved for the next pass.
        """
        unresolved: list[StoredOrder] = self._read_state(
            lambda state: state.unresolved_entries(), []
        )
        for stored in unresolved:
            try:
                found = self._broker.get_order_by_client_id(stored.client_order_id)
            except BrokerError as exc:
                logger.warning(
                    "could not look up entry %s with an unknown outcome: %s",
                    stored.client_order_id,
                    exc,
                )
                continue
            if found is not None:
                self._write_state(functools.partial(_adopt, stored=stored, order=found))
            elif self._now() - stored.created_at >= _UNKNOWN_ENTRY_GRACE:
                self._write_state(
                    functools.partial(
                        StateStore.update_order,
                        client_order_id=stored.client_order_id,
                        status="rejected",
                    )
                )
                logger.warning(
                    "entry %s for %s was never placed: the broker does not have it",
                    stored.client_order_id,
                    stored.symbol,
                )

    def _cancel_stale_entries(self, open_orders: Sequence[Order]) -> None:
        cutoff = self._now() - timedelta(minutes=self._settings.entry_ttl_minutes)
        for order in open_orders:
            if not _is_open_entry(order) or order.created_at >= cutoff:
                continue
            try:
                self._broker.cancel_order(order.id)
            except BrokerError as exc:
                logger.warning("could not cancel stale entry %s: %s", order.client_order_id, exc)
                continue
            logger.info(
                "cancelled stale entry %s for %s (%d of %d filled)",
                order.client_order_id,
                order.symbol,
                order.filled_qty,
                order.qty,
            )

    def _protect_positions(
        self,
        positions: Sequence[Position],
        orders: Sequence[Order],
        open_entries: Sequence[JournalEntry],
    ) -> list[ProtectionRepair]:
        """Cancel trader-broker's own standalone stops that cover more than is held,
        on every symbol that has one, held or not; then cover every position's
        unprotected shares and return the repairs."""
        held_qty = {position.symbol: position.qty for position in positions}
        for symbol in {order.symbol for order in orders if _is_own_standalone_stop(order)}:
            excess_qty = _stop_covered_qty(symbol, orders) - held_qty.get(symbol, 0)
            if excess_qty > 0:
                self._cancel_excess_stops(symbol, orders, excess_qty)

        newest_entry: dict[str, JournalEntry] = {}
        for entry in _held_entries(open_entries, orders):  # newest first: the first wins
            newest_entry.setdefault(entry.symbol, entry)

        repairs = []
        for position in positions:
            uncovered_qty = position.qty - _stop_covered_qty(position.symbol, orders)
            if uncovered_qty <= 0:
                continue
            confirmed = self._cancel_sells_without_stop(position.symbol, orders)
            journal_entry = newest_entry.get(position.symbol)
            repairs.append(
                self._protect(
                    position,
                    uncovered_qty,
                    _default_stop(position, journal_entry),
                    journal_entry.id if journal_entry else None,
                    may_sell=confirmed,
                )
            )
        return repairs

    def _cancel_sells_without_stop(self, symbol: str, orders: Sequence[Order]) -> bool:
        """Cancel `symbol`'s open sell orders that hold back held shares no stop guards.

        These are take-profit legs whose stop leg is not working and plain limit
        sells. The broker reserves the shares for them, so a repair stop or sell for
        those shares would be refused on every pass. Waits for the cancels to be
        confirmed and returns whether they were (True when there was nothing to
        cancel); a failure is logged and the repair is attempted anyway.
        """
        guarded_brackets = {
            order.parent_id
            for order in orders
            if _is_working(order) and order.type in _STOP_TYPES and order.parent_id is not None
        }
        unguarded = [
            order
            for order in _held_share_sells(symbol, orders)
            if order.type not in _STOP_TYPES and order.parent_id not in guarded_brackets
        ]
        if not unguarded:
            return True
        confirmed = self._cancel_and_confirm(unguarded)
        logger.warning(
            "cancelled %d %s sell orders that held shares back without a stop%s",
            len(unguarded),
            symbol,
            "" if confirmed else "; the broker has not confirmed every cancel",
        )
        return confirmed

    def _cancel_excess_stops(self, symbol: str, orders: Sequence[Order], excess_qty: int) -> None:
        """Cancel trader-broker's standalone `symbol` stops, newest first, while they
        cover `excess_qty` shares more than are held.

        Excess cover arises when a bracket's legs start working after housekeeping
        stopped the shares they could not yet cover, or when a leg sells shares a
        repair stop also covers; left alone, both could sell the same shares. A stop
        whose remaining qty is larger than the excess left is kept, since cancelling
        it would uncover held shares. Bracket legs are never cancelled here. A refusal
        is logged.
        """
        for order in orders:
            if excess_qty <= 0:
                return
            remaining_qty = order.qty - order.filled_qty
            if (
                order.symbol != symbol
                or not _is_own_standalone_stop(order)
                or remaining_qty > excess_qty
            ):
                continue
            try:
                self._broker.cancel_order(order.id)
            except BrokerError as exc:
                logger.warning("could not cancel excess stop %s: %s", order.client_order_id, exc)
                continue
            excess_qty -= remaining_qty
            logger.warning(
                "cancelled stop %s for %d %s: other stops already cover the shares",
                order.client_order_id,
                remaining_qty,
                symbol,
            )

    def _protect(
        self,
        position: Position,
        uncovered_qty: int,
        stop_price: Decimal,
        journal_entry_id: int | None,
        *,
        may_sell: bool = True,
    ) -> ProtectionRepair:
        """Place a GTC stop for `uncovered_qty` shares, or market-sell them when
        `may_sell` and the bid is real and at or below the stop.

        A missing quote or a bid of zero (no quote on the data feed) is no price, so
        the stop is placed. `may_sell` is False while cancels of orders that reserve
        the shares are unconfirmed, so a sale can never add to a sell still working.
        """
        symbol = position.symbol
        action: Literal["stop_placed", "sold"]
        bid = self._bid_or_none(symbol)
        try:
            if may_sell and bid is not None and bid <= stop_price:
                action = "sold"
                order = self._submit_recorded(
                    symbol,
                    "sell",
                    "sell",
                    lambda client_id: self._broker.submit_sell(symbol, uncovered_qty, client_id),
                    journal_entry_id=journal_entry_id,
                )
                logger.warning(
                    "sold %d unprotected %s at market: bid %s is at or below stop %s",
                    uncovered_qty,
                    symbol,
                    bid,
                    stop_price,
                )
            else:
                action = "stop_placed"
                order = self._submit_recorded(
                    symbol,
                    "sell",
                    "repair",
                    lambda client_id: self._broker.submit_stop(
                        symbol, uncovered_qty, stop_price, client_id
                    ),
                    journal_entry_id=journal_entry_id,
                )
                logger.warning(
                    "placed GTC stop at %s for %d unprotected %s", stop_price, uncovered_qty, symbol
                )
        except BrokerError as exc:
            logger.error("could not protect %d %s: %s", uncovered_qty, symbol, exc)
            return ProtectionRepair(
                symbol=symbol,
                qty=position.qty,
                uncovered_qty=uncovered_qty,
                stop_price=stop_price,
                action="failed",
                error=str(exc),
            )
        return ProtectionRepair(
            symbol=symbol,
            qty=position.qty,
            uncovered_qty=uncovered_qty,
            stop_price=stop_price,
            action=action,
            client_order_id=order.client_order_id,
        )

    def _bid_or_none(self, symbol: str) -> Decimal | None:
        """`symbol`'s bid, or None when there is no usable one (no quote, or zero)."""
        try:
            bid = self._broker.get_quote(symbol).bid_price
        except BrokerError as exc:
            logger.warning("no quote for %s: %s", symbol, exc)
            return None
        return bid if bid > 0 else None

    def _reconcile(
        self,
        account: Account,
        positions: Sequence[Position],
        orders: Sequence[Order],
        open_entries: Sequence[JournalEntry],
    ) -> None:
        """Copy broker statuses into state and record integrity violations.

        A violation is only: an open order (a bracket counts once) or a position
        trader-broker did not place; an entry whose sizing at placement broke a cap;
        negative cash. A position that grew past the symbol cap through price moves is
        not one. Each violation is recorded once, however many passes see it.
        """
        state = self._state
        if state is None:
            return
        now = self._now()
        orders_by_id = {order.id: order for order in orders}
        journaled_qty: dict[str, int] = {}
        for entry in open_entries:
            journaled_qty[entry.symbol] = journaled_qty.get(entry.symbol, 0) + entry.qty

        def violation(kind: str, subject: str, detail: str) -> None:
            if state.record_integrity_violation(now, kind, subject, detail):
                logger.error("integrity violation %s on %s: %s", kind, subject, detail)

        try:
            for order in orders:
                if not _placed_by_us(order, orders_by_id):
                    # A foreign bracket is one violation, reported on its entry.
                    if order.is_open and order.parent_id not in orders_by_id:
                        violation(
                            "foreign_order",
                            order.id,
                            f"open {order.side} {order.type} order for {order.qty} "
                            f"{order.symbol} was not placed by trader-broker",
                        )
                    continue
                stored = state.order(order.client_order_id)
                if stored is None:
                    continue  # a leg (recorded via its entry) or an order from a replaced database
                if stored.status != order.status or stored.broker_order_id is None:
                    state.update_order(order.client_order_id, order.status, order.id)
                if stored.role == "entry" and stored.sizing is not None:
                    broken = self._broken_caps(stored.sizing)
                    if broken:
                        violation("entry_over_cap", order.client_order_id, broken)

            for position in positions:
                journaled = journaled_qty.get(position.symbol, 0)
                if position.qty > journaled:
                    violation(
                        "foreign_position",
                        position.symbol,
                        f"holding {position.qty} {position.symbol}, but trader-broker's "
                        f"open journal entries account for {journaled}",
                    )

            if account.cash < 0:
                violation(
                    "negative_cash",
                    market_date(now).isoformat(),
                    f"cash is {account.cash}: margin is in use",
                )
        except StateUnavailableError as exc:
            logger.warning("state unavailable; reconciliation skipped: %s", exc)

    def _settle_journal(self, positions: Sequence[Position], orders: Sequence[Order]) -> None:
        """Close and settle journal entries (`journal.settle_trades`); log any failure."""
        if self._state is None:
            return
        try:
            settle_trades(self._state, self._broker, positions, orders, self._now())
        except (BrokerError, StateUnavailableError) as exc:
            logger.warning("journal settlement skipped: %s", exc)

    def _broken_caps(self, sizing: SizedOrder) -> str:
        """The caps an entry's sizing at placement broke, as text; empty when none."""
        broken = []
        if Decimal(str(sizing.pct_equity)) > self._caps.symbol_cap_pct:
            broken.append(f"{sizing.pct_equity:.2%} of equity is over the symbol cap")
        if Decimal(str(sizing.pct_equity_risk)) > self._caps.risk_pct:
            broken.append(f"{sizing.pct_equity_risk:.2%} of equity at risk is over the risk cap")
        return "; ".join(broken)

    # Buying

    def propose_buy(self, proposal: Proposal) -> BuyResult:
        """Size, check, review and place one bracket buy for `proposal`.

        In order: housekeeping; the risk gate (state must be available, no halt may
        be set, no earlier buy of the symbol may have an unknown outcome, and the buy
        is sized in code and checked against every entry rule, its prices against the
        quote); the cross-model review on a packet the server builds, whose accepted
        changes can only raise the stop or shrink the size; a re-check of the final
        order; then one GTC bracket (entry limit, take-profit at the target, stop at
        the final stop) under a new client order id recorded in state first, with
        what is needed to journal it. A lost submit reply is resolved by looking the
        id up, never by submitting again; when the broker cannot show the order yet,
        the result is `unknown` and housekeeping resolves it later.

        Every block is also written to the blocks table, with its rule ids. A broker
        failure before the order is placed blocks with `broker_error`; missing state
        with `state_unavailable`. A placed buy is journaled, linked to its stored review.
        """
        outcomes: list[ReviewOutcome] = []
        try:
            gate = self._check_buy(proposal)
            packet = self._review_packet(proposal, gate)
            outcomes = run_reviewers(self._reviewers, packet)
            order, review_id, applied = self._apply_reviews(gate, packet, outcomes)
            placed = self._submit_buy(proposal, order, review_id)
        except _BuyBlocked as blocked:
            rules, reasons = blocked.rules, blocked.reasons
        except _BuyOutcomeUnknown as unknown:
            logger.warning(
                "buy of %s has an unknown outcome: %s",
                proposal.symbol,
                unknown,
                extra={"event": "buy_unknown", "symbol": proposal.symbol},
            )
            return BuyResult(
                status="unknown",
                reasons=[str(unknown)],
                reviews=[_review_summary(outcome) for outcome in outcomes],
            )
        except StateUnavailableError as exc:
            rules, reasons = ["state_unavailable"], [f"state_unavailable: {exc}"]
        except BrokerError as exc:
            rules, reasons = ["broker_error"], [f"broker_error: {exc}"]
        else:
            logger.info(
                "placed bracket buy %s: %d %s at %s, stop %s, target %s",
                placed.client_order_id,
                placed.qty,
                placed.symbol,
                placed.limit_price,
                placed.stop_price,
                placed.target_price,
            )
            return BuyResult(
                status="placed",
                reasons=applied,
                order=placed,
                reviews=[_review_summary(outcome) for outcome in outcomes],
            )

        logger.warning(
            "blocked buy of %s: %s",
            proposal.symbol,
            "; ".join(reasons),
            extra={
                "event": "buy_blocked",
                "symbol": proposal.symbol,
                "rules": rules,
                "reasons": reasons,
            },
        )
        self._write_state(
            lambda state: state.add_block(
                self._now(), "propose_buy", proposal.symbol, rules, reasons
            )
        )
        return BuyResult(
            status="blocked",
            reasons=reasons,
            reviews=[_review_summary(outcome) for outcome in outcomes],
        )

    def _check_buy(self, proposal: Proposal) -> _GatePassed:
        """Run housekeeping, then the risk gate; raise `_BuyBlocked` with every failure.

        Halts, an unresolved earlier buy of the symbol, sizing and entry rules are all
        checked, so one block names them all.
        """
        halts = self.housekeeping().reading.halts
        if halts is None:
            raise _BuyBlocked(
                ["state_unavailable"],
                [
                    "state_unavailable: the state database is unavailable, so the halts "
                    "cannot be checked; `trader-broker init-state` creates one"
                ],
            )
        symbol = proposal.symbol
        unknown = [
            stored.client_order_id
            for stored in self._required_state().unresolved_entries()
            if stored.symbol == symbol
        ]
        portfolio = Portfolio(
            self._broker.get_account(),
            self._broker.get_positions(),
            self._broker.get_orders("open", limit=_ORDER_HISTORY_LIMIT),
        )
        asset = self._broker.get_asset(symbol)
        quote = self._broker.get_quote(symbol)
        daily_bars = self._broker.get_daily_bars(symbol, _DAILY_BAR_COUNT)
        clock = self._broker.get_clock()

        entry = entry_price(proposal.limit_price, quote.ask_price, self._caps)
        sizing = size_position(
            symbol, entry, proposal.stop_price, proposal.target_price, portfolio, self._caps
        )
        if sizing.sized is None:
            entry_check = check_market_rules(
                entry, round_price(proposal.stop_price), quote, asset, clock, daily_bars, self._caps
            )
        else:
            entry_check = check_entry(
                sizing.sized, portfolio, quote, asset, clock, daily_bars, self._caps
            )
        rules: list[BlockRule] = [*halts.failures, *sizing.failures, *entry_check.failures]
        reasons = halts.reasons + sizing.reasons + entry_check.reasons
        if unknown:
            rules.append("pending_unknown")
            reasons.append(
                f"pending_unknown: the outcome of an earlier {symbol} buy "
                f"({', '.join(unknown)}) is not known yet; housekeeping resolves it by its "
                "client order id"
            )
        if sizing.sized is None or reasons:
            raise _BuyBlocked(rules, reasons)
        logger.info(
            "risk gate passed %s: %d shares at %s, stop %s, bound by %s",
            symbol,
            sizing.sized.qty,
            sizing.sized.entry_price,
            sizing.sized.stop_price,
            sizing.sized.bound_by,
            extra={"event": "risk_decision", "symbol": symbol, "allowed": True},
        )
        return _GatePassed(sizing.sized, portfolio, asset, quote, daily_bars)

    def _review_packet(self, proposal: Proposal, gate: _GatePassed) -> ReviewPacket:
        """Read the news and SPY's bars, and build the packet the reviewers see."""
        news = self._broker.get_news([proposal.symbol], MAX_HEADLINES)
        spy_bars = self._broker.get_daily_bars(BENCHMARK_SYMBOL, _DAILY_BAR_COUNT)
        if not spy_bars:
            raise _BuyBlocked(
                ["broker_error"],
                [f"broker_error: no {BENCHMARK_SYMBOL} daily bars for the regime"],
            )
        return build_packet(
            proposal,
            gate.sized,
            gate.quote,
            gate.daily_bars,
            news,
            gate.portfolio,
            spy_bars,
        )

    def _apply_reviews(
        self, gate: _GatePassed, packet: ReviewPacket, outcomes: Sequence[ReviewOutcome]
    ) -> tuple[SizedOrder, int, list[str]]:
        """Decide on the reviews, store them, and re-check the final order.

        Returns the final order, the stored review's id and the changes applied.
        Raises `_BuyBlocked` when the reviews block the buy or the final order fails
        an entry rule, and `StateUnavailableError` when the review cannot be stored.
        """
        decision = decide(outcomes, gate.sized, gate.portfolio, self._caps)
        verdict = "proceed" if decision.proceed else "block"
        logger.info(
            "review decision on %s: %s; %s",
            gate.sized.symbol,
            verdict,
            "; ".join(decision.reasons) or "no changes",
            extra={
                "event": "review_decision",
                "symbol": gate.sized.symbol,
                "proceed": decision.proceed,
                "verdicts": {outcome.reviewer: outcome.verdict_name for outcome in outcomes},
            },
        )
        review_id = self._required_state().add_review(
            self._now(), packet.sha256, outcomes, verdict, decision.order
        )
        if decision.order is None:
            raise _BuyBlocked(["review"], decision.reasons)
        # The reviews can take minutes, so the clock is read again for the re-check.
        recheck = check_entry(
            decision.order,
            gate.portfolio,
            gate.quote,
            gate.asset,
            self._broker.get_clock(),
            gate.daily_bars,
            self._caps,
        )
        if not recheck.allowed:
            raise _BuyBlocked(recheck.failures, recheck.reasons)
        return decision.order, review_id, decision.reasons

    def _submit_buy(self, proposal: Proposal, order: SizedOrder, review_id: int) -> PlacedBuy:
        """Record the bracket's client id with what is needed to journal it, submit it
        once, and journal it.

        The intent must be stored before submitting: `StateUnavailableError` when it
        cannot be. A broker refusal raises `_BuyBlocked` with `broker_error`. A lost
        reply whose order the broker cannot show raises `_BuyOutcomeUnknown` and
        leaves the intent unresolved for housekeeping; nothing is resubmitted. Once
        placed, the journal entry is written when state allows.
        """
        client_order_id = new_client_order_id()
        opened_at = self._now()
        self._required_state().record_order_intent(
            client_order_id,
            order.symbol,
            "buy",
            "entry",
            opened_at,
            sizing=order,
            proposal=proposal,
            review_id=review_id,
        )
        request = BracketOrderRequest(
            symbol=order.symbol,
            qty=order.qty,
            limit_price=order.entry_price,
            stop_price=order.stop_price,
            take_profit_price=order.target_price,
            client_order_id=client_order_id,
        )
        try:
            entry = self._submit_once(
                client_order_id, lambda _: self._broker.submit_bracket_buy(request)
            )
        except UnknownResultError as exc:
            raise _BuyOutcomeUnknown(
                f"the submit result of bracket buy {client_order_id} is unknown ({exc}) and "
                "the broker does not show it yet; it was not resubmitted. Housekeeping "
                "journals it if the broker has it, and new buys of "
                f"{order.symbol} are blocked until then"
            ) from exc
        except BrokerError as exc:
            raise _BuyBlocked(
                ["broker_error"], [f"broker_error: bracket buy {client_order_id} failed: {exc}"]
            ) from exc

        self._write_state(
            lambda state: record_entry(
                state, proposal, order, client_order_id, review_id, opened_at
            )
        )
        return PlacedBuy(
            symbol=order.symbol,
            qty=order.qty,
            limit_price=order.entry_price,
            stop_price=order.stop_price,
            target_price=order.target_price,
            client_order_id=client_order_id,
            order_id=entry.id,
            status=entry.status,
        )

    # Journal

    def get_journal(
        self, status: JournalStatus | None = None, limit: int = 50
    ) -> list[JournalEntry]:
        """Refresh equity and settle the journal, then up to `limit` journal entries,
        newest first, with `status` (None: any).

        Settling first closes the entries of symbols no longer held, however the
        shares left (a sell tool, a stop or target filled at the broker), so an
        entry never reads as open after its position is gone. `pending_lessons`
        gives settled trades whose lesson is not recorded yet. Raises
        StateUnavailableError when there is no readable state.
        """
        self.refresh_equity()
        self._settle_journal(
            self._broker.get_positions(),
            self._broker.get_orders("all", limit=_ORDER_HISTORY_LIMIT),
        )
        return self._required_state().journal_entries(status, limit)

    def journal_note(self, entry_id: int, note: str) -> JournalEntry:
        """Run housekeeping, then store `note` as the entry's lesson and clear its flag.

        Returns the updated entry. Raises KeyError when there is no entry with
        `entry_id` and StateUnavailableError when there is no writable state.
        """
        self.housekeeping()
        state = self._required_state()
        state.set_journal_lesson(entry_id, note)
        entry = state.journal_entry(entry_id)
        assert entry is not None
        return entry

    # Exit tools. Halts and missing state never block them.

    def sell(self, symbol: str, qty: int) -> ExitResult:
        """Run housekeeping, then market-sell `qty` shares of `symbol`, good for the day.

        The symbol's open sell orders that reserve held shares (stops, and the legs of
        entries that have bought shares) are cancelled first and the cancels
        confirmed; when the broker does not confirm them in time, nothing is sold
        (`cancel_unconfirmed`), and the shares no working stop covers any more get a
        GTC stop before it returns (never a sale, since a cancelled sell may still be
        working). An open entry that has bought nothing keeps its bracket, so
        an add-on buy stays protected if it fills later. Shares left after the sale
        get a GTC stop at the highest of the cancelled stops, or, when there was
        none, at the stop housekeeping would choose; when the bid is already at or
        below that stop they are sold too. Rejects `qty` below 1 (`invalid_qty`) and
        `qty` above the shares held (`exceeds_position`): the server never shorts.
        """
        return self._sell(symbol, qty)

    def close_position(self, symbol: str) -> ExitResult:
        """`sell` every share of `symbol` held; rejected `no_position` when none are.

        Also cancels the symbol's open entries, with their brackets, so no buy can
        reopen the position.
        """
        return self._sell(symbol, qty=None)

    def tighten_stop(self, symbol: str, new_stop: Decimal) -> ExitResult:
        """Run housekeeping, then raise every stop order protecting held `symbol` shares
        to `new_stop`.

        `new_stop` is rounded to the tick. The stop legs of an entry that has bought
        nothing yet protect no shares, so they are neither counted nor changed. Only
        plain `stop` orders can be moved, the only stop type trader-broker places.
        Rejects with `no_stop` when no open stop order protects held shares,
        `unsupported_stop` when one of them is another stop type (a stop limit or
        trailing stop placed outside trader-broker), `stop_not_raised` when `new_stop`
        is not above the highest of those stops (a stop is never lowered), and
        `stop_at_or_above_bid` when it would trigger at once. On success the stop
        recorded in the open journal entries of the bought shares becomes `new_stop`.
        """
        self.housekeeping()
        new_stop = round_price(new_stop)
        open_orders = self._broker.get_orders("open", limit=_ORDER_HISTORY_LIMIT)
        stops = [
            order for order in _held_share_sells(symbol, open_orders) if order.type in _STOP_TYPES
        ]
        if not stops:
            return _rejected("no_stop", f"no open stop order protects {symbol}")
        unsupported = [order for order in stops if order.type != "stop"]
        if unsupported:
            return _rejected(
                "unsupported_stop",
                f"{symbol} is also protected by a {unsupported[0].type} order "
                f"({unsupported[0].client_order_id}) that trader-broker did not place and "
                "cannot move; change or cancel it at the broker first",
            )
        current_stop = max(order.stop_price or Decimal(0) for order in stops)
        if new_stop <= current_stop:
            return _rejected(
                "stop_not_raised",
                f"new stop {new_stop} is not above the current stop {current_stop}; "
                "a stop can only be raised",
            )
        bid = self._broker.get_quote(symbol).bid_price
        if new_stop >= bid:
            return _rejected(
                "stop_at_or_above_bid",
                f"new stop {new_stop} is at or above the bid {bid}; it would trigger at once",
            )

        entry_ids = [
            entry.id
            for entry in _held_entries(self._open_journal_entries(), open_orders)
            if entry.symbol == symbol
        ]
        replaced: list[Order] = []
        try:
            for order in stops:
                replaced.append(
                    self._submit_recorded(
                        symbol,
                        "sell",
                        "stop",
                        functools.partial(self._broker.replace_stop, order.id, new_stop),
                        journal_entry_id=entry_ids[0] if entry_ids else None,
                    )
                )
        except BrokerError as exc:
            logger.error("could not raise %s stop to %s: %s", symbol, new_stop, exc)
            return ExitResult(
                status="failed",
                reason="broker_error",
                message=f"raised {len(replaced)} of {len(stops)} {symbol} stops to "
                f"{new_stop}; the broker refused the next: {exc}",
                orders=replaced,
            )

        def record_stop(state: StateStore) -> None:
            for entry_id in entry_ids:
                state.set_journal_stop(entry_id, new_stop)

        self._write_state(record_stop)
        logger.info("raised %s stop from %s to %s", symbol, current_stop, new_stop)
        return ExitResult(
            status="done",
            message=f"raised {symbol} stop from {current_stop} to {new_stop}",
            orders=replaced,
        )

    def cancel_entry_order(self, order_id: str) -> ExitResult:
        """Run housekeeping, then cancel an open, unfilled buy entry trader-broker placed.

        Cancelling the entry cancels its legs. Everything else is rejected with
        `not_cancellable`: a stop or take-profit leg, a partly filled entry (its legs
        protect the shares bought), an order trader-broker did not place, and a closed
        or unknown order.
        """
        result = self.housekeeping()
        order = next((order for order in result.orders if order.id == order_id), None)
        if order is None:
            return _rejected("not_cancellable", f"no recent order has id {order_id}")
        refusal = _entry_cancel_refusal(order)
        if refusal is not None:
            return _rejected("not_cancellable", refusal)
        try:
            self._broker.cancel_order(order.id)
        except BrokerError as exc:
            return ExitResult(
                status="failed",
                reason="broker_error",
                message=f"the broker refused to cancel order {order_id}: {exc}",
            )
        logger.info("cancelled entry %s for %d %s", order.client_order_id, order.qty, order.symbol)
        return ExitResult(
            status="done",
            message=f"cancelled the entry for {order.qty} {order.symbol} and its legs",
        )

    def _sell(self, symbol: str, qty: int | None) -> ExitResult:
        """`sell` `qty` shares of `symbol`; None sells the whole position."""
        self.housekeeping()
        position = next(
            (position for position in self._broker.get_positions() if position.symbol == symbol),
            None,
        )
        held = position.qty if position else 0
        if qty is None and position is None:
            return _rejected("no_position", f"no {symbol} position is held")
        if qty is not None and qty < 1:
            return _rejected("invalid_qty", f"qty must be at least 1, got {qty}")
        if position is None or (qty is not None and qty > held):
            return _rejected(
                "exceeds_position", f"cannot sell {qty} {symbol}: {held} held, no shorts"
            )
        sell_qty = held if qty is None else qty

        open_orders = self._broker.get_orders("open", limit=_ORDER_HISTORY_LIMIT)
        reserving = _held_share_sells(symbol, open_orders)
        previous_stops = [
            order.stop_price
            for order in reserving
            if order.type in _STOP_TYPES and order.stop_price is not None
        ]
        if qty is None:
            # Cancelling an entry cancels its legs, or, once it has bought shares, only
            # its unfilled remainder; those legs are already in `reserving`.
            reserving += [
                order for order in open_orders if order.symbol == symbol and _is_open_entry(order)
            ]
        journal_entry = next(
            (
                entry
                for entry in _held_entries(self._open_journal_entries(), open_orders)
                if entry.symbol == symbol
            ),
            None,
        )
        journal_entry_id = journal_entry.id if journal_entry else None
        stop_price = max(previous_stops, default=_default_stop(position, journal_entry))
        logger.info(
            "selling %d of %d %s; cancelling %d open orders, previous stop %s",
            sell_qty,
            held,
            symbol,
            len(reserving),
            stop_price,
        )

        if not self._cancel_and_confirm(reserving):
            repairs = self._reprotect(symbol, stop_price, journal_entry_id)
            message = (
                f"the broker did not confirm cancelling the open {symbol} orders in time; "
                "nothing was sold"
            )
            if any(repair.action == "failed" for repair in repairs):
                message += f"; shares are unprotected until the cancels finish: {_RETRY_PROTECTION}"
            return ExitResult(
                status="failed",
                reason="cancel_unconfirmed",
                message=message,
                protection_repairs=repairs,
            )
        try:
            sale = self._submit_recorded(
                symbol,
                "sell",
                "sell",
                lambda client_id: self._broker.submit_sell(symbol, sell_qty, client_id),
                journal_entry_id=journal_entry_id,
            )
        except BrokerError as exc:
            logger.error("could not sell %d %s: %s", sell_qty, symbol, exc)
            # The cancelled orders protect nothing now, so protect every share again.
            return ExitResult(
                status="failed",
                reason="broker_error",
                message=f"the broker refused to sell {sell_qty} {symbol}: {exc}",
                protection_repairs=[self._protect(position, held, stop_price, journal_entry_id)],
            )

        remaining_qty = held - sell_qty
        repairs = []
        message = f"sold {sell_qty} {symbol} at market"
        if remaining_qty:
            repair = self._protect(position, remaining_qty, stop_price, journal_entry_id)
            repairs.append(repair)
            message += f"; {remaining_qty} left, protected: {repair.action}"
        return ExitResult(status="done", message=message, orders=[sale], protection_repairs=repairs)

    def _reprotect(
        self, symbol: str, stop_price: Decimal, journal_entry_id: int | None
    ) -> list[ProtectionRepair]:
        """After unconfirmed cancels, give `symbol`'s shares that no working stop covers
        a GTC stop at `stop_price`; never sell, since orders whose cancel is
        unconfirmed may still sell the shares.

        First waits, up to `_REPROTECT_WAIT_ATTEMPTS` reads, until none of `symbol`'s
        sells is being cancelled, then reads the position. Returns the repair, if one
        was needed. A repair the broker refuses while cancels are still pending is
        `failed`, with an error telling the agent how the next housekeeping pass
        retries it; a failed read is logged and leaves the work to that pass.
        """
        try:
            for attempt in range(_REPROTECT_WAIT_ATTEMPTS):
                if attempt:
                    self._sleep(_CANCEL_CONFIRM_INTERVAL_S)
                orders = self._broker.get_orders("all", limit=_ORDER_HISTORY_LIMIT)
                cancelling = [
                    order
                    for order in orders
                    if order.symbol == symbol
                    and order.side == "sell"
                    and order.status == "pending_cancel"
                ]
                if not cancelling:
                    break
            position = next(
                (held for held in self._broker.get_positions() if held.symbol == symbol), None
            )
        except BrokerError as exc:
            logger.error("could not re-read %s to re-protect it: %s", symbol, exc)
            return []
        if position is None:
            return []
        uncovered_qty = position.qty - _stop_covered_qty(symbol, orders)
        if uncovered_qty <= 0:
            return []
        repair = self._protect(
            position, uncovered_qty, stop_price, journal_entry_id, may_sell=False
        )
        if repair.action == "failed" and cancelling:
            repair = repair.model_copy(
                update={
                    "error": f"{repair.error}; {len(cancelling)} {symbol} orders being "
                    f"cancelled still hold the shares; {_RETRY_PROTECTION}"
                }
            )
        return [repair]

    def _cancel_and_confirm(self, orders: Sequence[Order]) -> bool:
        """Cancel `orders`, then wait until the broker lists none of them as open.

        A refused cancel is only logged: the order may have closed meanwhile, as a
        bracket leg does when its sibling is cancelled. Returns False when some order
        is still open after the bounded wait, or when the broker cannot be read to
        confirm: the orders may still be working.
        """
        for order in orders:
            try:
                self._broker.cancel_order(order.id)
            except BrokerError as exc:
                logger.warning("could not cancel %s: %s", order.client_order_id, exc)
        pending = {order.id for order in orders}
        for attempt in range(_CANCEL_CONFIRM_ATTEMPTS):
            if not pending:
                return True
            if attempt:
                self._sleep(_CANCEL_CONFIRM_INTERVAL_S)
            try:
                still_open = self._broker.get_orders("open", limit=_ORDER_HISTORY_LIMIT)
            except BrokerError as exc:
                logger.warning("could not confirm cancels: %s", exc)
                return False
            pending &= {order.id for order in still_open}
        return not pending

    # State and order helpers

    def _submit_recorded(
        self,
        symbol: str,
        side: OrderSide,
        role: OrderRole,
        submit: Callable[[str], Order],
        *,
        journal_entry_id: int | None = None,
    ) -> Order:
        """Record a new client order id in state when it is available, then submit once.

        Recording first means a lost reply can be looked up by client id; see
        `_submit_once`.
        """
        client_order_id = new_client_order_id()
        self._write_state(
            lambda state: state.record_order_intent(
                client_order_id,
                symbol,
                side,
                role,
                self._now(),
                journal_entry_id=journal_entry_id,
            )
        )
        return self._submit_once(client_order_id, submit)

    def _submit_once(self, client_order_id: str, submit: Callable[[str], Order]) -> Order:
        """Call `submit` once with an already recorded `client_order_id`.

        When the broker's reply is lost (`UnknownResultError`), the order is looked up
        by its client id: found, it counts as placed; not found, or the lookup fails,
        `UnknownResultError` is raised and nothing is resubmitted. A refusal is stored
        as `rejected` and re-raised. The broker's acknowledgement is stored when state
        is available.
        """
        try:
            order = submit(client_order_id)
        except UnknownResultError as unknown:
            try:
                found = self._broker.get_order_by_client_id(client_order_id)
            except BrokerError as exc:
                raise UnknownResultError(f"{unknown}; looking it up failed: {exc}") from exc
            if found is None:
                raise
            order = found
        except BrokerError:
            self._write_state(lambda state: state.update_order(client_order_id, "rejected"))
            raise
        self._write_state(lambda state: state.update_order(client_order_id, order.status, order.id))
        return order

    def _open_journal_entries(self) -> list[JournalEntry]:
        """Every open journal entry, newest first; empty without state."""
        return self._read_state(lambda state: state.journal_entries("open", limit=None), [])

    def _read_state[T](self, read: Callable[[StateStore], T], default: T) -> T:
        """`read(state)`, or `default` when state is unavailable."""
        if self._state is None:
            return default
        try:
            return read(self._state)
        except StateUnavailableError as exc:
            logger.warning("state unavailable; using no stored data: %s", exc)
            return default

    def _required_state(self) -> StateStore:
        """The state store, for writes a buy cannot go without.

        Raises StateUnavailableError when there is none.
        """
        if self._state is None:
            raise StateUnavailableError("no state database is open")
        return self._state

    def _write_state(self, write: Callable[[StateStore], object]) -> None:
        """`write(state)`, skipped with a warning when state is unavailable."""
        if self._state is None:
            logger.warning("state unavailable; write skipped")
            return
        try:
            write(self._state)
        except StateUnavailableError as exc:
            logger.warning("state unavailable; write skipped: %s", exc)


def _review_summary(outcome: ReviewOutcome) -> ReviewSummary:
    verdict = outcome.verdict
    return ReviewSummary(
        reviewer=outcome.reviewer,
        verdict=outcome.verdict_name,
        confidence=verdict.confidence if verdict else None,
        reasons=verdict.reasons if verdict else [outcome.error or "no verdict"],
    )


def _adopt(state: StateStore, stored: StoredOrder, order: Order) -> None:
    """Store the broker's `order` for the unresolved entry `stored`, and journal it from
    the proposal, sizing and review recorded with its intent, unless it is journaled
    already (`propose_buy` journals a placed buy even when storing its status failed)."""
    state.update_order(stored.client_order_id, order.status, order.id)
    if state.journal_entry_for_order(stored.client_order_id) is not None:
        return
    if stored.proposal is None or stored.sizing is None:
        logger.error("entry %s has no recorded proposal to journal", stored.client_order_id)
        return
    entry = record_entry(
        state,
        stored.proposal,
        stored.sizing,
        stored.client_order_id,
        stored.review_id,
        stored.created_at,
    )
    logger.warning(
        "entry %s for %s with an unknown outcome was placed; journaled as entry %d",
        stored.client_order_id,
        stored.symbol,
        entry.id,
    )


def _rejected(reason: ExitReason, message: str) -> ExitResult:
    return ExitResult(status="rejected", reason=reason, message=message)


def _default_stop(position: Position, journal_entry: JournalEntry | None) -> Decimal:
    """The stop for shares no stop covers: the journal entry's recorded stop, else
    `DEFAULT_STOP_PCT` below the position's average entry price."""
    if journal_entry is not None:
        return journal_entry.stop_price
    return round_price(position.avg_entry_price * (1 - DEFAULT_STOP_PCT))


def _entry_cancel_refusal(order: Order) -> str | None:
    """Why `cancel_entry_order` may not cancel `order`; None when it may."""
    if order.parent_id is not None:
        return "it is a stop or take-profit leg; use tighten_stop, sell or close_position"
    if order.side != "buy":
        return "it is a sell order, not a buy entry"
    if not order.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX):
        return "trader-broker did not place it"
    if not order.is_open:
        return f"it is already {order.status}"
    if order.filled_qty > 0:
        return (
            f"it has filled {order.filled_qty} of {order.qty} shares, which its legs protect; "
            "housekeeping cancels the unfilled remainder when the entry expires"
        )
    return None


def _is_open_entry(order: Order) -> bool:
    """True for an open buy that trader-broker placed (a bracket entry), not a leg."""
    return (
        order.is_open
        and order.side == "buy"
        and order.parent_id is None
        and order.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)
    )


def _held_share_sells(symbol: str, orders: Sequence[Order]) -> list[Order]:
    """`symbol`'s open sell orders in `orders` that reserve shares already held.

    Every open stop or limit sell counts except a leg of an entry, listed in
    `orders`, that has bought nothing: that leg guards shares not yet bought. Market
    sells are left out, since they fill at once.
    """
    unfilled_entries = {order.id for order in _unfilled_buys(orders)}
    return [
        order
        for order in orders
        if order.symbol == symbol
        and order.side == "sell"
        and order.type != "market"
        and order.is_open
        and order.parent_id not in unfilled_entries
    ]


def _held_entries(entries: Sequence[JournalEntry], orders: Sequence[Order]) -> list[JournalEntry]:
    """The journal `entries` whose buy has bought shares, in the given order.

    An entry whose bracket entry is in `orders` with nothing filled is left out; one
    missing from `orders` is assumed bought, as it is older than the orders read.
    """
    unfilled = {order.client_order_id for order in _unfilled_buys(orders)}
    return [entry for entry in entries if entry.client_order_id not in unfilled]


def _unfilled_buys(orders: Sequence[Order]) -> list[Order]:
    """The buy orders in `orders` that have bought nothing, open or closed."""
    return [order for order in orders if order.side == "buy" and order.filled_qty == 0]


def _placed_by_us(order: Order, orders_by_id: dict[str, Order]) -> bool:
    """True when trader-broker placed `order`, or it is a leg of a bracket it placed.

    A replaced stop carries a new `tb-` client id; a leg carries a broker-generated
    client id, so it is recognised by its entry.
    """
    if order.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX):
        return True
    parent = orders_by_id.get(order.parent_id) if order.parent_id else None
    return parent is not None and parent.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)


def _stop_covered_qty(symbol: str, orders: Sequence[Order]) -> int:
    """Shares of `symbol` that working stop sell orders cover, bracket legs included.

    A stop being cancelled (`pending_cancel`) covers nothing. A leg whose entry is in
    `orders` covers nothing while the entry is open and not completely filled, since
    the broker holds such legs inactive; after that it covers no more than the entry
    has filled, less what its legs (the take-profit included) have sold. A leg whose
    entry is not in `orders` counts in full, because a broker may list a working leg
    without its filled entry.
    """
    orders_by_id = {order.id: order for order in orders}
    legs_filled_qty: dict[str, int] = {}
    for order in orders:
        if order.parent_id is not None:
            legs_filled_qty[order.parent_id] = (
                legs_filled_qty.get(order.parent_id, 0) + order.filled_qty
            )
    covered = 0
    for order in orders:
        if (
            order.symbol != symbol
            or order.side != "sell"
            or order.type not in _STOP_TYPES
            or not _is_working(order)
        ):
            continue
        remaining = order.qty - order.filled_qty
        parent = orders_by_id.get(order.parent_id) if order.parent_id else None
        if parent is not None:
            if parent.is_open and parent.filled_qty < parent.qty:
                continue
            remaining = min(remaining, parent.filled_qty - legs_filled_qty[parent.id])
        covered += max(remaining, 0)
    return covered


def _is_own_standalone_stop(order: Order) -> bool:
    """True for a working stop sell that trader-broker placed outside a bracket: a
    repair stop or a raised one, never a bracket leg."""
    return (
        order.side == "sell"
        and order.type in _STOP_TYPES
        and order.parent_id is None
        and _is_working(order)
        and order.client_order_id.startswith(CLIENT_ORDER_ID_PREFIX)
    )


def _is_working(order: Order) -> bool:
    """True for an open order that is not being cancelled."""
    return order.is_open and order.status != "pending_cancel"
