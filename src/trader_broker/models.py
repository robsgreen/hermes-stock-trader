"""Data types shared by the broker adapters, the risk gate, the reviewers and the journal.

Prices and money amounts are `Decimal` so order prices are exact; shares-of-a-whole
(such as 0.20 for 20%) are `float`, as in `Settings`. Share quantities are whole
shares: the server never trades fractions. Every timestamp is timezone-aware.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MAX_PRICE = Decimal(1_000_000)
"""Highest price, in USD per share, any order may name: above every US share price the
account could buy, and small enough that price arithmetic stays exact."""

Price = Annotated[Decimal, Field(gt=0, le=MAX_PRICE)]
"""A finite price in USD per share, above zero and at most `MAX_PRICE`."""

_CENT = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")


def round_price(price: Decimal) -> Decimal:
    """Round a price to the broker's tick: $0.01 at or above $1, $0.0001 below. Half up."""
    tick = _CENT if price >= 1 else _SUB_DOLLAR_TICK
    return price.quantize(tick, rounding=ROUND_HALF_UP)


OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop"]
OrderClass = Literal["simple", "bracket", "oco", "oto"]
TimeInForce = Literal["day", "gtc", "opg", "cls", "ioc", "fok"]
OrderStatus = Literal[
    "new",
    "accepted",
    "pending_new",
    "accepted_for_bidding",
    "held",
    "partially_filled",
    "filled",
    "done_for_day",
    "pending_cancel",
    "pending_replace",
    "stopped",
    "suspended",
    "calculated",
    "canceled",
    "expired",
    "replaced",
    "rejected",
]
"""Alpaca's order statuses; adapters for other brokers map onto these."""

_CLOSED_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {"filled", "canceled", "expired", "replaced", "rejected"}
)
"""Statuses after which an order can no longer fill; every other status is open."""

RiskRule = Literal[
    "long_only",
    "leverage",
    "cash",
    "symbol_cap",
    "risk_size",
    "min_price",
    "min_adv",
    "market_closed",
    "too_late",
    "daily_halt",
    "kill_switch",
    "limit_invalid",
    "stop_invalid",
    "target_invalid",
    "qty_zero",
    "not_tradable",
    "state_unavailable",
    "pending_unknown",
]
"""Id of a rule that can block a buy."""

BlockRule = Literal[RiskRule, "broker_error", "review"]
"""Why a buy was blocked: a risk rule, a broker failure, or the cross-model review."""

Verdict = Literal["approve", "modify", "reject"]
"""A reviewer's answer to a proposed buy."""

VerdictName = Literal[Verdict, "error"]
"""A reviewer's verdict, or `error` when it gave no valid verdict."""

SizingLimit = Literal["risk_size", "symbol_cap", "cash"]
"""A limit that can set a buy's share count when the server sizes it."""


class _Record(BaseModel):
    """Base for immutable records."""

    model_config = ConfigDict(frozen=True)


class Account(_Record):
    equity: Decimal
    last_equity: Decimal
    """Equity at the previous close, as the broker reports it."""
    cash: Decimal
    """Settled and unsettled cash; negative means margin is in use."""
    buying_power: Decimal


class Position(_Record):
    symbol: str
    qty: int
    """Shares held; positive, since the server never shorts."""
    avg_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal


class Order(_Record):
    """One broker order. A bracket is three orders: the entry and its two legs."""

    id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    order_class: OrderClass
    status: OrderStatus
    qty: int
    filled_qty: int = 0
    filled_avg_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce
    parent_id: str | None = None
    """Id of the bracket entry this order is a stop or take-profit leg of; None otherwise."""
    created_at: AwareDatetime

    @property
    def is_open(self) -> bool:
        """True while the order can still fill (including a leg held for its entry)."""
        return self.status not in _CLOSED_ORDER_STATUSES


class Quote(_Record):
    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    timestamp: AwareDatetime


class Bar(_Record):
    timestamp: AwareDatetime
    """Start of the bar's period."""
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class NewsItem(_Record):
    id: str
    headline: str
    summary: str = ""
    source: str
    symbols: list[str]
    url: str | None = None
    created_at: AwareDatetime


class Mover(_Record):
    """One symbol on a top gainers or losers list."""

    symbol: str
    price: Decimal
    change: Decimal
    """Change since the previous close, in USD per share."""
    change_pct: float
    """Change since the previous close, as a share of that close (0.05 means up 5%)."""


class Movers(_Record):
    gainers: list[Mover]
    losers: list[Mover]


class Clock(_Record):
    timestamp: AwareDatetime
    is_open: bool
    next_open: AwareDatetime
    next_close: AwareDatetime


class TradingDay(_Record):
    """One trading session from the market calendar."""

    date: date
    open: AwareDatetime
    close: AwareDatetime
    """Session close; earlier than usual on an early-close day."""


class Asset(_Record):
    symbol: str
    asset_class: str
    """`us_equity` for US stocks and ETFs."""
    exchange: str
    status: Literal["active", "inactive"]
    tradable: bool


class EquityPoint(_Record):
    """Account equity at one point of the broker's portfolio history."""

    timestamp: AwareDatetime
    equity: Decimal


class Fill(_Record):
    id: str
    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    price: Decimal
    filled_at: AwareDatetime


@dataclass(frozen=True)
class BracketOrderRequest:
    """A limit buy with a take-profit limit leg and a stop-loss leg, all whole shares."""

    symbol: str
    qty: int
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    client_order_id: str
    time_in_force: Literal["gtc"] = "gtc"


class Proposal(_Record):
    """A buy the agent proposes. It names prices and reasons but never a share count."""

    symbol: str = Field(min_length=1)
    stop_price: Price
    target_price: Price
    thesis: str = Field(min_length=1, max_length=2000)
    invalidation: str = Field(min_length=1, max_length=500)
    """What would prove the thesis wrong."""
    horizon_days: int = Field(ge=1, le=60)
    limit_price: Price | None = None
    """Entry limit; None means a marketable limit derived from the ask."""


class SizedOrder(_Record):
    """A buy after the server has sized it."""

    symbol: str
    qty: int
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    notional: Decimal
    """qty * entry_price."""
    pct_equity: float
    """notional as a share of equity."""
    risk_usd: Decimal
    """Loss if the stop fills at its price: qty * (entry_price - stop_price)."""
    pct_equity_risk: float
    """risk_usd as a share of equity."""
    bound_by: Literal[SizingLimit, "review"]
    """What set qty: a sizing limit, or `review` when a reviewer's size multiplier or
    raised stop cut it."""

    @classmethod
    def build(
        cls,
        symbol: str,
        qty: int,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        equity: Decimal,
        bound_by: Literal[SizingLimit, "review"],
    ) -> Self:
        """The sized order with its money fields derived from the prices, `qty` and
        `equity` (positive)."""
        notional = qty * entry_price
        risk_usd = qty * (entry_price - stop_price)
        return cls(
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            notional=notional,
            pct_equity=float(notional / equity),
            risk_usd=risk_usd,
            pct_equity_risk=float(risk_usd / equity),
            bound_by=bound_by,
        )


class RiskDecision(_Record):
    """Outcome of the risk gate: every failing rule, not only the first."""

    allowed: bool
    failures: list[RiskRule]
    sized: SizedOrder | None
    reasons: list[str] = Field(default_factory=list)
    """Plain-language explanations of the failures, with the numbers that broke each rule."""

    @model_validator(mode="after")
    def _allowed_matches_failures(self) -> Self:
        if self.allowed == bool(self.failures):
            raise ValueError("allowed must be True exactly when failures is empty")
        return self


class Modifications(_Record):
    """Changes a reviewer asks for. The server applies only those that reduce risk."""

    stop_price: Price | None = None
    size_multiplier: float | None = Field(None, gt=0, le=1)
    target_price: Price | None = None
    """Advisory only: recorded, never applied."""


class ReviewVerdict(_Record):
    """A reviewer's validated reply."""

    verdict: Verdict
    confidence: Literal["low", "medium", "high"]
    reasons: list[str] = Field(min_length=1, max_length=5)
    modifications: Modifications | None = None


class ReviewOutcome(_Record):
    """One reviewer's result: a verdict, or an error after its retry also failed."""

    reviewer: str
    verdict: ReviewVerdict | None = None
    error: str | None = None
    """Why no verdict was obtained (timeout, API error, invalid reply)."""

    @model_validator(mode="after")
    def _verdict_or_error(self) -> Self:
        if (self.verdict is None) == (self.error is None):
            raise ValueError("exactly one of verdict and error must be set")
        return self

    @property
    def verdict_name(self) -> VerdictName:
        """The verdict given, or `error` when there is none."""
        return "error" if self.verdict is None else self.verdict.verdict


class JournalEntry(_Record):
    """A placed buy with the reasons for it and, once closed, how it did."""

    id: int
    symbol: str
    client_order_id: str
    """Client order id of the bracket entry."""
    opened_at: AwareDatetime
    qty: int
    """Shares ordered; once closed, the shares the entry actually bought when the
    broker reported its fill."""
    entry_price: Decimal
    """The entry limit; once closed, the entry's average fill price when the broker
    reported one."""
    stop_price: Decimal
    """Current protective stop; raised when the stop is tightened."""
    target_price: Decimal
    thesis: str
    invalidation: str
    horizon_days: int
    review_id: int | None = None
    status: Literal["open", "closed"] = "open"
    closed_at: AwareDatetime | None = None
    exit_price: Decimal | None = None
    """Qty-weighted average sell price; None while open, and for a buy that never filled."""
    trade_return_pct: float | None = None
    """(exit_price - entry_price) / entry_price, e.g. 0.10 for +10%."""
    spy_return_pct: float | None = None
    """SPY's return over the same holding window."""
    alpha_pct: float | None = None
    """trade_return_pct - spy_return_pct."""
    lesson_pending: bool = False
    """True once the trade is settled and until a lesson is recorded."""
    lesson: str | None = None
