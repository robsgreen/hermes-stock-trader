from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader_broker.config import Settings
from trader_broker.models import (
    Account,
    Asset,
    Bar,
    Clock,
    Order,
    Position,
    Quote,
    RiskDecision,
    SizedOrder,
)
from trader_broker.risk import Caps, Portfolio, check_entry, check_market_rules

CAPS = Caps.from_settings(Settings())
NOW = datetime(2026, 11, 3, 15, 30, tzinfo=UTC)
CLOSE = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
OPEN_CLOCK = Clock(timestamp=NOW, is_open=True, next_open=NOW + timedelta(days=1), next_close=CLOSE)
TRADABLE = Asset(
    symbol="XYZ", asset_class="us_equity", exchange="NASDAQ", status="active", tradable=True
)


def _sized(qty: int = 50, entry: str = "100", stop: str = "95") -> SizedOrder:
    entry_price, stop_price = Decimal(entry), Decimal(stop)
    notional = qty * entry_price
    risk_usd = qty * (entry_price - stop_price)
    return SizedOrder(
        symbol="XYZ",
        qty=qty,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=entry_price * 2,
        notional=notional,
        pct_equity=float(notional / 100_000),
        risk_usd=risk_usd,
        pct_equity_risk=float(risk_usd / 100_000),
        bound_by="risk_size",
    )


def _portfolio(
    cash: int = 100_000, positions_value: int = 0, open_buy_notional: int = 0
) -> Portfolio:
    account = Account(
        equity=Decimal(100_000),
        last_equity=Decimal(100_000),
        cash=Decimal(cash),
        buying_power=Decimal(cash),
    )
    positions = [
        Position(
            symbol="ABC",
            qty=positions_value // 100,
            avg_entry_price=Decimal(100),
            current_price=Decimal(100),
            market_value=Decimal(positions_value),
        )
    ]
    orders = [
        Order(
            id="order-1",
            client_order_id="tb-1",
            symbol="DEF",
            side="buy",
            type="limit",
            order_class="bracket",
            status="new",
            qty=open_buy_notional // 100,
            limit_price=Decimal(100),
            time_in_force="gtc",
            created_at=NOW,
        )
    ]
    return Portfolio(account, positions, orders)


def _daily_bars(close: int = 100, volume: int = 1_000_000) -> list[Bar]:
    return [
        Bar(
            timestamp=NOW - timedelta(days=day),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=volume,
        )
        for day in range(20, 0, -1)
    ]


def _check(
    sized: SizedOrder | None = None,
    portfolio: Portfolio | None = None,
    asset: Asset | None = TRADABLE,
    clock: Clock = OPEN_CLOCK,
    daily_bars: Sequence[Bar] | None = None,
    quote: Quote | None = None,
) -> RiskDecision:
    """`check_entry`; the default quote has bid and ask at the entry price."""
    sized = sized or _sized()
    return check_entry(
        sized,
        portfolio or _portfolio(),
        quote or _quote(sized.entry_price, sized.entry_price),
        asset,
        clock,
        _daily_bars() if daily_bars is None else daily_bars,
        CAPS,
    )


def _quote(bid: Decimal | str, ask: Decimal | str) -> Quote:
    return Quote(symbol="XYZ", bid_price=Decimal(bid), ask_price=Decimal(ask), timestamp=NOW)


def test_a_buy_within_every_limit_is_allowed() -> None:
    decision = _check()

    assert decision.allowed
    assert decision.failures == []
    assert decision.sized == _sized()


def test_leverage_counts_positions_and_pending_buys() -> None:
    portfolio = _portfolio(cash=100_000, positions_value=95_000, open_buy_notional=3_000)

    assert _check(portfolio=portfolio).failures == ["leverage"]


def test_buy_over_available_cash_fails_cash() -> None:
    portfolio = _portfolio(cash=10_000, open_buy_notional=6_000)

    assert _check(portfolio=portfolio).failures == ["cash"]


def test_buy_over_the_symbol_cap_fails_symbol_cap() -> None:
    assert _check(_sized(qty=201, entry="100", stop="99")).failures == ["symbol_cap"]


def test_loss_at_stop_over_the_risk_limit_fails_risk_size() -> None:
    assert _check(_sized(qty=150, entry="100", stop="75")).failures == ["risk_size"]


def test_price_below_minimum_fails_min_price() -> None:
    decision = _check(_sized(qty=100, entry="4.99", stop="4.70"))

    assert decision.failures == ["min_price"]


def test_thin_dollar_volume_fails_min_adv() -> None:
    decision = _check(daily_bars=_daily_bars(close=10, volume=500_000))  # $5M a day

    assert decision.failures == ["min_adv"]
    assert "$5,000,000.00" in decision.reasons[0]


def test_short_bar_history_fails_min_adv() -> None:
    assert _check(daily_bars=_daily_bars()[1:]).failures == ["min_adv"]


def test_twenty_minutes_before_close_is_too_late() -> None:
    clock = OPEN_CLOCK.model_copy(update={"timestamp": CLOSE - timedelta(minutes=20)})

    assert _check(clock=clock).failures == ["too_late"]


def test_thirty_minutes_before_close_is_allowed() -> None:
    clock = OPEN_CLOCK.model_copy(update={"timestamp": CLOSE - timedelta(minutes=30)})

    assert _check(clock=clock).allowed


def test_closed_market_fails_market_closed() -> None:
    clock = Clock(
        timestamp=CLOSE + timedelta(hours=2),
        is_open=False,
        next_open=NOW + timedelta(days=1),
        next_close=CLOSE + timedelta(days=1),
    )

    assert _check(clock=clock).failures == ["market_closed"]


@pytest.mark.parametrize(
    "asset",
    [
        None,
        TRADABLE.model_copy(update={"status": "inactive"}),
        TRADABLE.model_copy(update={"tradable": False}),
        TRADABLE.model_copy(update={"asset_class": "crypto"}),
    ],
)
def test_untradable_asset_fails_not_tradable(asset: Asset | None) -> None:
    assert _check(asset=asset).failures == ["not_tradable"]


def test_every_failing_rule_is_named() -> None:
    clock = OPEN_CLOCK.model_copy(update={"is_open": False})
    inactive = TRADABLE.model_copy(update={"status": "inactive"})

    decision = _check(_sized(qty=100, entry="4.99", stop="4.70"), clock=clock, asset=inactive)

    assert not decision.allowed
    assert decision.failures == ["min_price", "market_closed", "not_tradable"]
    assert len(decision.reasons) == 3
    assert decision.sized is not None


def test_market_rules_need_no_sized_order() -> None:
    closed = OPEN_CLOCK.model_copy(update={"is_open": False})

    decision = check_market_rules(
        Decimal("4.99"), Decimal("4.70"), _quote("4.99", "4.99"), None, closed, [], CAPS
    )

    assert decision.sized is None
    assert decision.failures == ["min_price", "min_adv", "market_closed", "not_tradable"]


def test_limit_above_the_ask_plus_slippage_fails_limit_invalid() -> None:
    # The ask is 99; a limit of 100 is more than 0.5% above it.
    decision = _check(quote=_quote("98.98", "99"))

    assert decision.failures == ["limit_invalid"]
    assert "99.50" in decision.reasons[0]


def test_limit_below_the_ask_is_allowed() -> None:
    assert _check(quote=_quote("101.98", "102")).allowed


def test_stop_at_or_above_the_bid_fails_stop_invalid() -> None:
    # The stock fell through the stop: bid 95 with the stop at 95.
    decision = _check(quote=_quote("95", "95.02"))

    assert "stop_invalid" in decision.failures


def test_no_bid_fails_stop_invalid() -> None:
    assert "stop_invalid" in _check(quote=_quote("0", "100")).failures


def test_min_price_judges_the_ask_when_it_is_below_the_limit() -> None:
    # A $3 stock with the agent's limit at $5: the limit alone would pass min_price.
    decision = _check(_sized(qty=100, entry="5", stop="2.80"), quote=_quote("2.99", "3"))

    assert "min_price" in decision.failures
    assert "limit_invalid" in decision.failures
