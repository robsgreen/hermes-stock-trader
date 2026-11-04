from decimal import Decimal

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import BracketOrderRequest, Order
from trader_broker.service import TraderService, new_client_order_id


def _bracket(broker: FakeBroker, client_order_id: str | None = None) -> Order:
    """An unfilled bracket buy of 100 XYZ at 95; trader-broker's unless a client id is given."""
    return broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=100,
            limit_price=Decimal(95),
            stop_price=Decimal(90),
            take_profit_price=Decimal(110),
            client_order_id=client_order_id or new_client_order_id(),
        )
    )


def _leg(broker: FakeBroker, entry: Order, order_type: str) -> Order:
    return next(
        order
        for order in broker.get_orders("all")
        if order.parent_id == entry.id and order.type == order_type
    )


def _status(broker: FakeBroker, order_id: str) -> str:
    return next(order.status for order in broker.get_orders("all") if order.id == order_id)


def test_own_unfilled_entry_is_cancelled_with_its_legs(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    entry = _bracket(fake_broker)

    result = trader_service.cancel_entry_order(entry.id)

    assert (result.status, result.reason) == ("done", None)
    assert fake_broker.get_orders("open") == []
    assert _status(fake_broker, entry.id) == "canceled"


@pytest.mark.parametrize("leg_type", ["stop", "limit"])
def test_bracket_legs_are_not_cancellable(
    trader_service: TraderService, fake_broker: FakeBroker, leg_type: str
) -> None:
    entry = _bracket(fake_broker)
    leg = _leg(fake_broker, entry, leg_type)

    result = trader_service.cancel_entry_order(leg.id)

    assert (result.status, result.reason) == ("rejected", "not_cancellable")
    assert _status(fake_broker, leg.id) == "held"


def test_partly_filled_entry_is_not_cancellable(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    entry = _bracket(fake_broker)
    fake_broker.fill_order(entry.id, qty=40)

    result = trader_service.cancel_entry_order(entry.id)

    assert (result.status, result.reason) == ("rejected", "not_cancellable")
    assert _status(fake_broker, entry.id) == "partially_filled"


def test_foreign_order_is_not_cancellable(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    entry = _bracket(fake_broker, client_order_id="manual-buy")

    result = trader_service.cancel_entry_order(entry.id)

    assert (result.status, result.reason) == ("rejected", "not_cancellable")
    assert _status(fake_broker, entry.id) == "new"


def test_unknown_order_is_not_cancellable(trader_service: TraderService) -> None:
    assert trader_service.cancel_entry_order("order-404").reason == "not_cancellable"
