from datetime import datetime
from decimal import Decimal

from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import BracketOrderRequest, Order
from trader_broker.service import TraderService, new_client_order_id
from trader_broker.state import HaltState, StateStore


def _bought_bracket(broker: FakeBroker, stop: Decimal = Decimal(90)) -> Order:
    """A filled trader-broker bracket buy of 100 XYZ at 100, its stop leg at `stop`."""
    entry = broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=100,
            limit_price=Decimal(100),
            stop_price=stop,
            take_profit_price=Decimal(120),
            client_order_id=new_client_order_id(),
        )
    )
    broker.fill_order(entry.id)
    return entry


def _journal(state: StateStore, now: datetime, stop: Decimal = Decimal(90)) -> int:
    return state.add_journal_entry(
        symbol="XYZ",
        client_order_id="tb-journaled",
        opened_at=now,
        qty=100,
        entry_price=Decimal(100),
        stop_price=stop,
        target_price=Decimal(120),
        thesis="breakout",
        invalidation="close below 95",
        horizon_days=10,
    ).id


def _open_stop_prices(broker: FakeBroker) -> list[Decimal | None]:
    return [order.stop_price for order in broker.get_orders("open") if order.type == "stop"]


def test_lowering_the_stop_is_rejected(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)

    result = trader_service.tighten_stop("XYZ", Decimal(88))

    assert (result.status, result.reason) == ("rejected", "stop_not_raised")
    assert _open_stop_prices(fake_broker) == [Decimal(90)]


def test_new_stop_at_or_above_the_bid_is_rejected(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)  # bid 99.98

    for new_stop in (Decimal("99.98"), Decimal(101)):
        result = trader_service.tighten_stop("XYZ", new_stop)
        assert (result.status, result.reason) == ("rejected", "stop_at_or_above_bid")
    assert _open_stop_prices(fake_broker) == [Decimal(90)]


def test_symbol_without_a_stop_is_rejected(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))
    fake_broker.fail_next("submit_stop")  # housekeeping cannot repair it

    result = trader_service.tighten_stop("XYZ", Decimal(95))

    assert (result.status, result.reason) == ("rejected", "no_stop")


def test_raising_the_stop_replaces_it_and_updates_the_journal(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry = _bought_bracket(fake_broker)
    journal_id = _journal(state_store, now)

    result = trader_service.tighten_stop("XYZ", Decimal(95))

    assert (result.status, result.reason) == ("done", None)
    [stop] = [order for order in fake_broker.get_orders("open") if order.type == "stop"]
    assert (stop.stop_price, stop.qty, stop.parent_id) == (Decimal(95), 100, entry.id)
    assert [order.id for order in result.orders] == [stop.id]
    journal_entry = state_store.journal_entry(journal_id)
    assert journal_entry is not None
    assert journal_entry.stop_price == Decimal(95)
    stored = state_store.order(stop.client_order_id)
    assert stored is not None
    assert (stored.role, stored.broker_order_id, stored.journal_entry_id) == (
        "stop",
        stop.id,
        journal_id,
    )


def test_new_stop_is_rounded_to_the_tick(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)

    trader_service.tighten_stop("XYZ", Decimal("95.125"))

    assert _open_stop_prices(fake_broker) == [Decimal("95.13")]


def test_raising_the_stop_is_accepted_while_the_kill_switch_is_set(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    _bought_bracket(fake_broker)
    state_store.update_halts(
        lambda _: HaltState(Decimal(200_000), kill_switch=True, daily_halt_date=None)
    )

    result = trader_service.tighten_stop("XYZ", Decimal(95))

    assert result.status == "done"
    assert _open_stop_prices(fake_broker) == [Decimal(95)]


def test_raised_stand_alone_stop_is_not_reported_as_foreign(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))
    _journal(state_store, now)
    trader_service.housekeeping()  # places a stand-alone GTC stop at the journal's 90

    result = trader_service.tighten_stop("XYZ", Decimal(95))
    trader_service.housekeeping()

    assert result.status == "done"
    [stop] = result.orders
    assert (stop.parent_id, stop.client_order_id[:3]) == (None, "tb-")
    assert _open_stop_prices(fake_broker) == [Decimal(95)]
    assert state_store.integrity_violation_count() == 0


def test_a_stop_type_it_cannot_move_is_rejected_with_a_clear_reason(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)
    # A trailing stop placed by hand at the broker; FakeBroker has no API to submit one.
    fake_broker._add_order(
        client_order_id="manual-trail",
        symbol="XYZ",
        side="sell",
        type="trailing_stop",
        order_class="simple",
        status="new",
        qty=10,
        stop_price=Decimal(89),
        time_in_force="gtc",
    )

    result = trader_service.tighten_stop("XYZ", Decimal(95))

    assert (result.status, result.reason) == ("rejected", "unsupported_stop")
    assert "trailing_stop" in result.message and "manual-trail" in result.message
    assert sorted(_open_stop_prices(fake_broker)) == [Decimal(90)]
