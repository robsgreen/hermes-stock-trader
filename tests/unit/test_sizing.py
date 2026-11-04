from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trader_broker.config import Settings
from trader_broker.models import Account, Order, OrderStatus, Position, RiskDecision, SizedOrder
from trader_broker.risk import Caps, Portfolio, entry_price, round_price, size_position

CAPS = Caps.from_settings(Settings())


def _portfolio(
    equity: int = 100_000,
    cash: int | None = None,
    positions: list[Position] | None = None,
    open_orders: list[Order] | None = None,
) -> Portfolio:
    account = Account(
        equity=Decimal(equity),
        last_equity=Decimal(equity),
        cash=Decimal(equity if cash is None else cash),
        buying_power=Decimal(equity),
    )
    return Portfolio(account, positions or [], open_orders or [])


def _position(symbol: str, qty: int, price: int) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=Decimal(price),
        current_price=Decimal(price),
        market_value=Decimal(qty * price),
    )


def _buy_order(
    symbol: str, qty: int, limit_price: int, filled_qty: int = 0, status: OrderStatus = "new"
) -> Order:
    return Order(
        id=f"order-{symbol}",
        client_order_id=f"tb-{symbol}",
        symbol=symbol,
        side="buy",
        type="limit",
        order_class="bracket",
        status=status,
        qty=qty,
        filled_qty=filled_qty,
        limit_price=Decimal(limit_price),
        time_in_force="gtc",
        created_at=datetime(2026, 11, 3, 15, 0, tzinfo=UTC),
    )


def _size(
    entry: str, stop: str, target: str = "200", portfolio: Portfolio | None = None
) -> RiskDecision:
    return size_position(
        "XYZ", Decimal(entry), Decimal(stop), Decimal(target), portfolio or _portfolio(), CAPS
    )


def _sized(decision: RiskDecision) -> SizedOrder:
    assert decision.allowed, decision.reasons
    assert decision.sized is not None
    return decision.sized


def test_symbol_cap_binds_over_risk_size() -> None:
    sized = _sized(_size("100", "90"))

    assert sized.qty == 200  # risk size would be 300
    assert sized.bound_by == "symbol_cap"
    assert sized.notional == Decimal(20_000)
    assert sized.pct_equity == pytest.approx(0.20)
    assert sized.risk_usd == Decimal(2_000)
    assert sized.pct_equity_risk == pytest.approx(0.02)


def test_tight_stop_is_capped_by_symbol_cap() -> None:
    sized = _sized(_size("100", "99"))

    assert sized.qty == 200
    assert sized.bound_by == "symbol_cap"
    assert sized.risk_usd == Decimal(200)


def test_wide_stop_is_bound_by_risk_size() -> None:
    sized = _sized(_size("50", "40", target="60"))

    assert sized.qty == 300
    assert sized.bound_by == "risk_size"
    assert sized.notional == Decimal(15_000)
    assert sized.risk_usd == Decimal(3_000)
    assert sized.pct_equity_risk == pytest.approx(0.03)


def test_cash_binds() -> None:
    sized = _sized(_size("100", "95", portfolio=_portfolio(cash=5_000)))

    assert sized.qty == 50
    assert sized.bound_by == "cash"


def test_existing_position_counts_toward_symbol_cap() -> None:
    portfolio = _portfolio(positions=[_position("XYZ", 150, 100)])

    assert _sized(_size("100", "90", portfolio=portfolio)).qty == 50


def test_open_entry_order_counts_toward_symbol_cap() -> None:
    portfolio = _portfolio(open_orders=[_buy_order("XYZ", 150, 100)])

    assert _sized(_size("100", "90", portfolio=portfolio)).qty == 50


def test_open_buy_orders_on_other_symbols_reduce_available_cash() -> None:
    portfolio = _portfolio(cash=20_000, open_orders=[_buy_order("ABC", 150, 100)])

    sized = _sized(_size("100", "90", portfolio=portfolio))

    assert sized.qty == 50  # $5k of cash is not committed to the ABC order
    assert sized.bound_by == "cash"


def test_only_the_unfilled_part_of_open_buys_counts() -> None:
    portfolio = _portfolio(
        cash=20_000,
        open_orders=[
            _buy_order("ABC", 150, 100, filled_qty=100),
            _buy_order("DEF", 150, 100, status="canceled"),
        ],
    )

    assert _sized(_size("100", "90", portfolio=portfolio)).qty == 150


def test_prices_are_rounded_to_the_cent() -> None:
    sized = _sized(_size("100.004", "90.005", target="120.0049"))

    assert (sized.entry_price, sized.stop_price, sized.target_price) == (
        Decimal("100.00"),
        Decimal("90.01"),
        Decimal("120.00"),
    )


@pytest.mark.parametrize("stop", ["100", "101"])
def test_stop_at_or_above_entry_is_invalid(stop: str) -> None:
    decision = _size("100", stop)

    assert decision.failures == ["stop_invalid"]
    assert decision.sized is None


@pytest.mark.parametrize("target", ["100", "100.01"])
def test_target_not_above_entry_plus_a_cent_is_invalid(target: str) -> None:
    assert _size("100", "90", target=target).failures == ["target_invalid"]


def test_invalid_stop_and_target_are_both_named() -> None:
    assert _size("100", "100", target="100").failures == ["stop_invalid", "target_invalid"]


@pytest.mark.parametrize("stop", ["99.60", "74"])  # 0.4% and 26% below entry
def test_stop_distance_outside_the_allowed_range_is_invalid(stop: str) -> None:
    decision = _size("100", stop)

    assert decision.failures == ["stop_invalid"]
    assert "stop distance" in decision.reasons[0]
    assert "0.5% to 25%" in decision.reasons[0]


def test_stop_distance_at_the_bounds_is_allowed() -> None:
    assert _size("100", "99.50").allowed
    assert _size("100", "75").allowed


def test_less_than_one_share_is_qty_zero() -> None:
    decision = _size("100", "90", portfolio=_portfolio(cash=99))

    assert decision.failures == ["qty_zero"]
    assert decision.sized is None


def test_symbol_over_its_cap_is_qty_zero() -> None:
    portfolio = _portfolio(positions=[_position("XYZ", 250, 100)])

    assert _size("100", "90", portfolio=portfolio).failures == ["qty_zero"]


def test_entry_price_uses_the_limit_when_given() -> None:
    assert entry_price(Decimal("101.234"), Decimal("100.02"), CAPS) == Decimal("101.23")


def test_entry_price_adds_slippage_to_the_ask() -> None:
    # 100.02 * 1.005 = 100.5201
    assert entry_price(None, Decimal("100.02"), CAPS) == Decimal("100.52")


@pytest.mark.parametrize(
    ("price", "rounded"),
    [("10.005", "10.01"), ("10.004", "10.00"), ("1", "1.00"), ("0.12345", "0.1235")],
)
def test_round_price(price: str, rounded: str) -> None:
    assert round_price(Decimal(price)) == Decimal(rounded)
