"""The risk gate: share sizing, entry checks and halt checks. Pure functions with no I/O.

Callers read the broker and the state store, then pass what they read in. Every check
reports all the rules a buy breaks, not only the first, so the agent sees every reason
at once.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Self

from trader_broker.config import Settings
from trader_broker.indicators import average_dollar_volume
from trader_broker.models import (
    Account,
    Asset,
    Bar,
    Clock,
    Order,
    Position,
    Quote,
    RiskDecision,
    RiskRule,
    SizedOrder,
    SizingLimit,
    round_price,
)

ADV_DAYS = 20
"""Number of daily bars the average dollar volume rule averages over."""

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Caps:
    """The limits the risk gate enforces, taken from `Settings` at server start.

    Shares of a whole (0.03 for 3%) are `Decimal` here so sizing arithmetic is exact.
    """

    risk_pct: Decimal
    """Share of equity a single trade may lose if its stop is hit."""
    symbol_cap_pct: Decimal
    """Largest share of equity one symbol's position plus open buys may take."""
    daily_loss_pct: Decimal
    """Loss from the previous close's equity that halts new buys for the day."""
    kill_drawdown_pct: Decimal
    """Drawdown from peak equity that trips the kill switch."""
    min_price: Decimal
    """Lowest entry price, in USD, the gate allows."""
    min_adv_usd: Decimal
    """Lowest 20-day average dollar volume, in USD, the gate allows."""
    entry_slippage_pct: Decimal
    """How far above the ask a marketable entry limit is set, as a share of the ask."""
    no_entry_before_close_minutes: int
    """Minutes before the close during which no buy may start."""
    min_stop_pct: Decimal
    """Smallest stop distance, as a share of the entry price."""
    max_stop_pct: Decimal
    """Largest stop distance, as a share of the entry price."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The caps `settings` configures."""

        def exact(value: float) -> Decimal:
            # Through str so 0.03 becomes Decimal("0.03"), not its binary approximation.
            return Decimal(str(value))

        return cls(
            risk_pct=exact(settings.risk_pct),
            symbol_cap_pct=exact(settings.symbol_cap_pct),
            daily_loss_pct=exact(settings.daily_loss_pct),
            kill_drawdown_pct=exact(settings.kill_drawdown_pct),
            min_price=exact(settings.min_price),
            min_adv_usd=exact(settings.min_adv_usd),
            entry_slippage_pct=exact(settings.entry_slippage_pct),
            no_entry_before_close_minutes=settings.no_entry_before_close_minutes,
            min_stop_pct=exact(settings.min_stop_pct),
            max_stop_pct=exact(settings.max_stop_pct),
        )


@dataclass(frozen=True)
class Portfolio:
    """The account, its positions and its open orders, as read from the broker together."""

    account: Account
    positions: Sequence[Position] = ()
    open_orders: Sequence[Order] = ()
    """Orders that can still fill; closed orders in the list are ignored."""

    @property
    def positions_value(self) -> Decimal:
        """Market value of every position."""
        return sum((position.market_value for position in self.positions), Decimal(0))

    def open_buy_notional(self, symbol: str | None = None) -> Decimal:
        """Value of the unfilled part of open buy orders, for one symbol or (None) all.

        Each order is valued at its limit price, or its stop price when it has no
        limit. A buy with neither (a market order) counts as zero: the server never
        places one, and housekeeping reports orders it did not place.
        """
        total = Decimal(0)
        for order in self.open_orders:
            if order.side != "buy" or not order.is_open:
                continue
            if symbol is not None and order.symbol != symbol:
                continue
            price = order.limit_price or order.stop_price or Decimal(0)
            total += (order.qty - order.filled_qty) * price
        return total

    def symbol_value(self, symbol: str) -> Decimal:
        """Market value held in `symbol` plus the notional of its open buy orders."""
        held = sum(
            (position.market_value for position in self.positions if position.symbol == symbol),
            Decimal(0),
        )
        return held + self.open_buy_notional(symbol)

    @property
    def available_cash(self) -> Decimal:
        """Cash not already committed to open buy orders on any symbol."""
        return self.account.cash - self.open_buy_notional()


def entry_price(limit_price: Decimal | None, ask: Decimal, caps: Caps) -> Decimal:
    """The entry limit for a buy, rounded to tick.

    `limit_price` when the proposal gives one; otherwise a marketable limit at
    ask * (1 + entry_slippage_pct).
    """
    if limit_price is not None:
        return round_price(limit_price)
    return marketable_limit(ask, caps)


def marketable_limit(ask: Decimal, caps: Caps) -> Decimal:
    """The highest entry limit the gate allows: ask * (1 + entry_slippage_pct), rounded."""
    return round_price(ask * (1 + caps.entry_slippage_pct))


def size_position(
    symbol: str,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    portfolio: Portfolio,
    caps: Caps,
) -> RiskDecision:
    """Size a bracket buy in whole shares, or say why it cannot be sized.

    Prices are rounded to tick first. The share count is the floor of the smallest of
    three limits: the loss at the stop within `risk_pct` of equity; the symbol's value
    (held plus open buys) within `symbol_cap_pct` of equity; and the cash not committed
    to open buys. The result is allowed with `sized` set, or blocked with failures among
    `stop_invalid` (stop not below entry, or stop distance outside the configured
    range), `target_invalid` (target not above entry + $0.01) and `qty_zero`.
    """
    entry, stop, target = round_price(entry), round_price(stop), round_price(target)
    failures = _Failures()

    if stop >= entry:
        failures.add("stop_invalid", f"stop {stop} is not below entry {entry}")
    else:
        distance_pct = (entry - stop) / entry
        if not caps.min_stop_pct <= distance_pct <= caps.max_stop_pct:
            failures.add(
                "stop_invalid",
                f"stop distance {_pct(distance_pct)} of entry is outside the allowed "
                f"{_pct(caps.min_stop_pct)} to {_pct(caps.max_stop_pct)}",
            )
    if target <= entry + _CENT:
        failures.add("target_invalid", f"target {target} must be above entry {entry} + 0.01")
    if failures:
        return failures.decision(sized=None)

    equity = portfolio.account.equity
    limits: dict[SizingLimit, Decimal] = {
        "risk_size": caps.risk_pct * equity / (entry - stop),
        "symbol_cap": (caps.symbol_cap_pct * equity - portfolio.symbol_value(symbol)) / entry,
        "cash": portfolio.available_cash / entry,
    }
    bound_by = min(limits, key=lambda name: limits[name])
    qty = math.floor(limits[bound_by])
    if qty < 1:
        failures.add("qty_zero", f"the {bound_by} limit allows {_shares(limits[bound_by])} shares")
        return failures.decision(sized=None)

    sized = SizedOrder.build(symbol, qty, entry, stop, target, equity, bound_by)
    return failures.decision(sized=sized)


def check_entry(
    sized: SizedOrder,
    portfolio: Portfolio,
    quote: Quote,
    asset: Asset | None,
    clock: Clock,
    daily_bars: Sequence[Bar],
    caps: Caps,
) -> RiskDecision:
    """Check a sized buy against every entry rule and name each one it breaks.

    `portfolio` is the state before this buy and `quote` the symbol's latest quote.
    `asset` is the broker's lookup of the symbol (None when unknown). `daily_bars` are
    SIP daily bars, oldest first, ending at the previous close; the last `ADV_DAYS` of
    them set the average dollar volume. Possible failures: `leverage`, `cash`,
    `symbol_cap`, `risk_size`, and those of `check_market_rules`. The decision
    carries `sized` either way.
    """
    failures = _Failures()
    equity = portfolio.account.equity
    notional = sized.notional

    committed = portfolio.positions_value + portfolio.open_buy_notional()
    if committed + notional > equity:
        failures.add(
            "leverage",
            f"positions and open buys {_usd(committed)} plus this buy {_usd(notional)} "
            f"exceed equity {_usd(equity)}",
        )
    if notional > portfolio.available_cash:
        failures.add(
            "cash",
            f"this buy {_usd(notional)} exceeds available cash {_usd(portfolio.available_cash)}",
        )
    symbol_cap = caps.symbol_cap_pct * equity
    symbol_value = portfolio.symbol_value(sized.symbol) + notional
    if symbol_value > symbol_cap:
        failures.add(
            "symbol_cap",
            f"{sized.symbol} would reach {_usd(symbol_value)}, over the "
            f"{_pct(caps.symbol_cap_pct)} cap of {_usd(symbol_cap)}",
        )
    risk_cap = caps.risk_pct * equity
    if sized.risk_usd > risk_cap:
        failures.add(
            "risk_size",
            f"loss at the stop {_usd(sized.risk_usd)} exceeds the "
            f"{_pct(caps.risk_pct)} limit of {_usd(risk_cap)}",
        )
    failures.extend(
        check_market_rules(
            sized.entry_price, sized.stop_price, quote, asset, clock, daily_bars, caps
        )
    )
    return failures.decision(sized=sized)


def check_market_rules(
    entry: Decimal,
    stop: Decimal,
    quote: Quote,
    asset: Asset | None,
    clock: Clock,
    daily_bars: Sequence[Bar],
    caps: Caps,
) -> RiskDecision:
    """Check the entry rules that hold whatever the share count, and name each broken.

    Also for a buy that could not be sized, so its block still lists every rule it
    breaks. `entry` is the entry limit and `stop` the stop price; `quote` is the
    symbol's quote, whose symbol is checked; the other arguments are as for
    `check_entry`. The prices are judged against the quote: the limit may not be
    above `marketable_limit` of the ask (`limit_invalid`), the stop must be below the
    bid (`stop_invalid`; no bid fails too), and the lower of the limit and the ask
    must reach `min_price`. Other possible failures: `min_adv`, `market_closed`,
    `too_late`, `not_tradable`. The decision never carries a sized order.
    """
    failures = _Failures()
    symbol = quote.symbol
    highest_limit = marketable_limit(quote.ask_price, caps)
    if entry > highest_limit:
        failures.add(
            "limit_invalid",
            f"limit {entry} is above the ask {quote.ask_price} plus "
            f"{_pct(caps.entry_slippage_pct)} ({highest_limit})",
        )
    if stop >= quote.bid_price:
        failures.add(
            "stop_invalid", f"stop {stop} is not below the bid {quote.bid_price}; it would trigger"
        )
    price = min(entry, quote.ask_price)
    if price < caps.min_price:
        failures.add(
            "min_price",
            f"the lower of the limit {entry} and the ask {quote.ask_price} is below the "
            f"minimum price {_usd(caps.min_price)}",
        )

    adv = average_dollar_volume(daily_bars, ADV_DAYS)
    if adv is None:
        failures.add("min_adv", f"fewer than {ADV_DAYS} daily bars to measure dollar volume")
    elif Decimal(str(adv)) < caps.min_adv_usd:
        failures.add(
            "min_adv",
            f"{ADV_DAYS}-day average dollar volume {_usd(Decimal(str(adv)))} is below "
            f"{_usd(caps.min_adv_usd)}",
        )

    if not clock.is_open:
        failures.add("market_closed", "the market is closed")
    cutoff = clock.next_close - timedelta(minutes=caps.no_entry_before_close_minutes)
    if clock.timestamp > cutoff:
        failures.add(
            "too_late",
            f"less than {caps.no_entry_before_close_minutes} minutes before the close "
            f"at {clock.next_close.isoformat()}",
        )

    if (
        asset is None
        or asset.status != "active"
        or not asset.tradable
        or asset.asset_class != "us_equity"
    ):
        failures.add("not_tradable", f"{symbol} is not an active, tradable US equity")

    return failures.decision(sized=None)


@dataclass(frozen=True)
class HaltCheck:
    """Halt state after a check, and the halts that block new buys now."""

    peak: Decimal
    """Highest equity seen, including this reading."""
    kill_switch: bool
    """True once drawdown from the peak has reached the limit; never cleared here."""
    daily_halt_date: date | None
    """Trading date of the last daily halt; buys are blocked while it equals today."""
    failures: list[RiskRule] = field(default_factory=list)
    """`daily_halt` and `kill_switch` when they block buys."""
    reasons: list[str] = field(default_factory=list)


def check_halts(
    equity: Decimal,
    last_equity: Decimal,
    peak: Decimal,
    kill_switch: bool,
    daily_halt_date: date | None,
    today: date,
    caps: Caps,
) -> HaltCheck:
    """Update the halt latches from the current equity and report what blocks buys.

    `last_equity` is the broker's equity at the previous close and `peak` the stored
    peak. The daily halt latches for `today` once the day's loss reaches
    `daily_loss_pct`, and keeps blocking for the rest of `today` even if equity
    recovers. The kill switch latches once equity is `kill_drawdown_pct` below the
    peak; only the CLI clears it. Callers persist the returned peak, kill switch and
    daily halt date.
    """
    peak = max(peak, equity)
    kill_switch = kill_switch or equity <= peak * (1 - caps.kill_drawdown_pct)
    if last_equity > 0 and (equity - last_equity) / last_equity <= -caps.daily_loss_pct:
        daily_halt_date = today

    failures = _Failures()
    if daily_halt_date == today:
        failures.add(
            "daily_halt",
            f"the day's loss reached the {_pct(caps.daily_loss_pct)} limit; "
            "buys resume next trading day",
        )
    if kill_switch:
        failures.add(
            "kill_switch",
            f"drawdown from the peak {_usd(peak)} reached {_pct(caps.kill_drawdown_pct)}; "
            "only `trader-broker reset-kill-switch` resumes buys",
        )
    return HaltCheck(
        peak=peak,
        kill_switch=kill_switch,
        daily_halt_date=daily_halt_date,
        failures=failures.rules,
        reasons=failures.reasons,
    )


class _Failures:
    """Failing rules with their reasons, collected in the order they were checked."""

    def __init__(self) -> None:
        self.rules: list[RiskRule] = []
        self.reasons: list[str] = []

    def __bool__(self) -> bool:
        return bool(self.rules)

    def add(self, rule: RiskRule, reason: str) -> None:
        self.rules.append(rule)
        self.reasons.append(f"{rule}: {reason}")

    def extend(self, decision: RiskDecision) -> None:
        """Add every failure `decision` names, after those already collected."""
        self.rules += decision.failures
        self.reasons += decision.reasons

    def decision(self, sized: SizedOrder | None) -> RiskDecision:
        return RiskDecision(
            allowed=not self.rules, failures=self.rules, sized=sized, reasons=self.reasons
        )


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _pct(share: Decimal) -> str:
    return f"{share * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _shares(qty: Decimal) -> str:
    return f"{qty:.2f}"
