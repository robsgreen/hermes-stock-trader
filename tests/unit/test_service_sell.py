from datetime import date, datetime
from decimal import Decimal

import pytest

from trader_broker.brokers.base import BrokerError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.models import BracketOrderRequest, Order
from trader_broker.service import TraderService, new_client_order_id
from trader_broker.state import HaltState, StateStore

TODAY = date(2026, 11, 3)


def _bought_bracket(broker: FakeBroker, stop: Decimal = Decimal(90), qty: int = 100) -> Order:
    """A filled trader-broker bracket buy of `qty` XYZ at 100, its stop leg at `stop`."""
    entry = broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=qty,
            limit_price=Decimal(100),
            stop_price=stop,
            take_profit_price=Decimal(120),
            client_order_id=new_client_order_id(),
        )
    )
    broker.fill_order(entry.id)
    return entry


def _journal(state: StateStore, now: datetime, stop: Decimal, qty: int = 100) -> int:
    return state.add_journal_entry(
        symbol="XYZ",
        client_order_id="tb-journaled",
        opened_at=now,
        qty=qty,
        entry_price=Decimal(100),
        stop_price=stop,
        target_price=Decimal(120),
        thesis="breakout",
        invalidation="close below 95",
        horizon_days=10,
    ).id


def _held(broker: FakeBroker) -> int:
    return sum(position.qty for position in broker.get_positions() if position.symbol == "XYZ")


def _open_sells(broker: FakeBroker) -> list[Order]:
    return [order for order in broker.get_orders("open") if order.side == "sell"]


def test_full_close_cancels_the_legs_and_sells_every_share(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _bought_bracket(fake_broker)
    journal_id = _journal(state_store, now, stop=Decimal(90))

    result = trader_service.close_position("XYZ")

    assert (result.status, result.reason) == ("done", None)
    assert _held(fake_broker) == 0
    assert _open_sells(fake_broker) == []
    assert result.protection_repairs == []
    [sale] = result.orders
    assert (sale.type, sale.time_in_force, sale.qty, sale.status) == (
        "market",
        "day",
        100,
        "filled",
    )
    stored = state_store.order(sale.client_order_id)
    assert stored is not None
    assert (stored.role, stored.status, stored.journal_entry_id) == ("sell", "filled", journal_id)


def test_partial_sell_re_protects_the_remaining_shares_at_the_previous_stop(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _bought_bracket(fake_broker, stop=Decimal("91.50"))
    _journal(state_store, now, stop=Decimal(85))  # an older stop: the order's price wins

    result = trader_service.sell("XYZ", 40)

    assert result.status == "done"
    assert _held(fake_broker) == 60
    [stop] = _open_sells(fake_broker)
    assert (stop.type, stop.qty, stop.stop_price, stop.time_in_force) == (
        "stop",
        60,
        Decimal("91.50"),
        "gtc",
    )
    assert stop.client_order_id.startswith("tb-")
    assert state_store.order(stop.client_order_id) is not None
    [repair] = result.protection_repairs
    assert (repair.uncovered_qty, repair.action) == (60, "stop_placed")


def test_sell_with_no_legs_protects_the_rest_with_the_housekeeping_stop(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _journal(state_store, now, stop=Decimal(88))
    fake_broker.set_position("XYZ", 100, Decimal(100))
    fake_broker.fail_next("submit_stop")  # housekeeping's repair is refused: no stop at all

    result = trader_service.sell("XYZ", 30)

    assert result.status == "done"
    assert _held(fake_broker) == 70
    [stop] = _open_sells(fake_broker)
    assert (stop.qty, stop.stop_price) == (70, Decimal(88))


def test_close_without_legs_or_journal_sells_everything(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    fake_broker.set_position("XYZ", 50, Decimal(100))
    fake_broker.fail_next("submit_stop")

    result = trader_service.close_position("XYZ")

    assert result.status == "done"
    assert _held(fake_broker) == 0


def test_selling_more_than_held_is_rejected_exceeds_position(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)

    result = trader_service.sell("XYZ", 101)

    assert (result.status, result.reason) == ("rejected", "exceeds_position")
    assert _held(fake_broker) == 100
    assert {order.type for order in _open_sells(fake_broker)} == {"limit", "stop"}


def test_selling_a_symbol_not_held_is_rejected_exceeds_position(
    trader_service: TraderService,
) -> None:
    assert trader_service.sell("XYZ", 1).reason == "exceeds_position"


@pytest.mark.parametrize("qty", [0, -5])
def test_qty_below_one_is_rejected(
    trader_service: TraderService, fake_broker: FakeBroker, qty: int
) -> None:
    _bought_bracket(fake_broker)

    result = trader_service.sell("XYZ", qty)

    assert (result.status, result.reason) == ("rejected", "invalid_qty")
    assert _held(fake_broker) == 100


def test_closing_a_symbol_not_held_is_rejected_no_position(
    trader_service: TraderService,
) -> None:
    assert trader_service.close_position("XYZ").reason == "no_position"


def test_broker_rejects_a_sell_while_legs_reserve_the_shares_so_the_service_cancels_first(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)

    with pytest.raises(BrokerError, match="insufficient qty"):
        fake_broker.submit_sell("XYZ", 100, new_client_order_id())

    assert trader_service.close_position("XYZ").status == "done"
    assert _held(fake_broker) == 0


def test_unconfirmed_cancels_sell_nothing(state_store: StateStore, now: datetime) -> None:
    class SlowCancelBroker(FakeBroker):
        def cancel_order(self, order_id: str) -> None:
            pass  # accepted, but the broker never reports the order closed

    broker = SlowCancelBroker(now)
    broker.set_account(cash=Decimal(100_000))
    broker.set_quote("XYZ", bid=Decimal("99.98"), ask=Decimal("100.02"))
    _bought_bracket(broker)
    sleeps: list[float] = []
    service = TraderService(
        broker, state_store, Settings(), [], now=lambda: now, sleep=sleeps.append
    )

    result = service.close_position("XYZ")

    assert (result.status, result.reason) == ("failed", "cancel_unconfirmed")
    assert _held(broker) == 100
    assert not any(order.type == "market" for order in broker.get_orders("all"))
    assert sleeps == [0.5] * 9


def _service_with_sleeps(
    broker: FakeBroker, state: StateStore, now: datetime
) -> tuple[TraderService, list[float]]:
    sleeps: list[float] = []
    service = TraderService(broker, state, Settings(), [], now=lambda: now, sleep=sleeps.append)
    return service, sleeps


def test_cancels_confirmed_on_a_later_poll_let_the_sale_go_ahead(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _bought_bracket(fake_broker)
    fake_broker.delay_cancels(reads=2)  # Alpaca cancels through pending_cancel
    service, sleeps = _service_with_sleeps(fake_broker, state_store, now)

    result = service.close_position("XYZ")

    assert result.status == "done"
    assert _held(fake_broker) == 0
    assert sleeps == [0.5, 0.5]  # confirmed on the third poll


def test_cancels_confirmed_too_late_get_the_shares_a_stop_and_never_a_sale(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _bought_bracket(fake_broker)
    fake_broker.set_price("XYZ", Decimal(89))  # below the stop: housekeeping would sell
    # Pending through all ten confirmation polls; cancelled by the next read.
    fake_broker.delay_cancels(reads=10)
    service, _ = _service_with_sleeps(fake_broker, state_store, now)

    result = service.close_position("XYZ")

    assert (result.status, result.reason) == ("failed", "cancel_unconfirmed")
    assert _held(fake_broker) == 100
    [repair] = result.protection_repairs
    assert (repair.uncovered_qty, repair.action, repair.stop_price) == (
        100,
        "stop_placed",
        Decimal(90),
    )
    [stop] = _open_sells(fake_broker)
    assert (stop.type, stop.qty, stop.parent_id) == ("stop", 100, None)


@pytest.mark.parametrize("cancel_reads", [12, 25])
def test_re_protection_waits_for_cancels_still_pending_after_the_confirmation_polls(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime, cancel_reads: int
) -> None:
    # A stop being cancelled still reserves its shares, so a repair stop placed
    # before the cancel finishes would be refused.
    _bought_bracket(fake_broker)
    fake_broker.delay_cancels(reads=cancel_reads)
    service, _ = _service_with_sleeps(fake_broker, state_store, now)

    result = service.close_position("XYZ")

    assert (result.status, result.reason) == ("failed", "cancel_unconfirmed")
    [repair] = result.protection_repairs
    assert (repair.uncovered_qty, repair.action) == (100, "stop_placed")
    [stop] = _open_sells(fake_broker)
    assert (stop.type, stop.qty, stop.parent_id) == ("stop", 100, None)


def test_cancels_pending_past_the_wait_report_a_failed_repair_that_housekeeping_retries(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _bought_bracket(fake_broker)
    _journal(state_store, now, stop=Decimal(90))
    fake_broker.set_price("XYZ", Decimal(89))  # below the stop: never sold while cancelling
    fake_broker.delay_cancels(reads=60)
    service, _ = _service_with_sleeps(fake_broker, state_store, now)

    result = service.close_position("XYZ")

    assert (result.status, result.reason) == ("failed", "cancel_unconfirmed")
    [repair] = result.protection_repairs
    assert (repair.uncovered_qty, repair.action) == (100, "failed")
    assert repair.error is not None and "get_risk_status" in repair.error
    assert "get_risk_status" in result.message
    assert _held(fake_broker) == 100
    assert not any(order.type == "market" for order in fake_broker.get_orders("all"))

    for _ in range(30):  # time passes; the broker finishes the cancels
        fake_broker.get_orders("all")
    fake_broker.set_price("XYZ", Decimal(95))
    [retried] = service.housekeeping().protection_repairs

    assert (retried.uncovered_qty, retried.action) == (100, "stop_placed")
    [stop] = _open_sells(fake_broker)
    assert (stop.qty, stop.stop_price, stop.parent_id) == (100, Decimal(90), None)


def test_a_failed_confirmation_poll_counts_as_unconfirmed_and_re_protects(
    state_store: StateStore, now: datetime
) -> None:
    class PollFailsAfterCancel(FakeBroker):
        def cancel_order(self, order_id: str) -> None:
            super().cancel_order(order_id)
            self.fail_next("get_orders")

    broker = PollFailsAfterCancel(now)
    broker.set_account(cash=Decimal(100_000))
    broker.set_quote("XYZ", bid=Decimal("99.98"), ask=Decimal("100.02"))
    _bought_bracket(broker)
    service, _ = _service_with_sleeps(broker, state_store, now)

    result = service.close_position("XYZ")

    assert (result.status, result.reason) == ("failed", "cancel_unconfirmed")
    assert _held(broker) == 100
    assert [repair.action for repair in result.protection_repairs] == ["stop_placed"]
    [stop] = _open_sells(broker)
    assert (stop.qty, stop.stop_price) == (100, Decimal(90))


def test_refused_sell_re_protects_every_share(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)
    fake_broker.fail_next("submit_sell")

    result = trader_service.sell("XYZ", 40)

    assert (result.status, result.reason) == ("failed", "broker_error")
    assert _held(fake_broker) == 100
    [stop] = _open_sells(fake_broker)
    assert (stop.qty, stop.stop_price) == (100, Decimal(90))


def test_sell_whose_reply_is_lost_is_found_by_client_id(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    _bought_bracket(fake_broker)
    fake_broker.accept_then_time_out("submit_sell")

    result = trader_service.close_position("XYZ")

    assert result.status == "done"
    assert [order.qty for order in result.orders] == [100]
    assert _held(fake_broker) == 0


# Halts and missing state never block an exit


def test_sell_succeeds_while_the_kill_switch_is_set(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    _bought_bracket(fake_broker)
    state_store.update_halts(
        lambda _: HaltState(Decimal(200_000), kill_switch=True, daily_halt_date=None)
    )

    result = trader_service.sell("XYZ", 40)

    assert result.status == "done"
    assert _held(fake_broker) == 60
    assert state_store.halts().kill_switch


def test_sell_succeeds_while_the_daily_halt_is_set(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    _bought_bracket(fake_broker)
    state_store.update_halts(
        lambda _: HaltState(Decimal(100_000), kill_switch=False, daily_halt_date=TODAY)
    )

    result = trader_service.close_position("XYZ")

    assert result.status == "done"
    assert _held(fake_broker) == 0
    assert state_store.halts().daily_halt_date == TODAY


def test_sell_succeeds_without_state(fake_broker: FakeBroker, now: datetime) -> None:
    _bought_bracket(fake_broker)
    service = TraderService(fake_broker, None, Settings(), [], now=lambda: now)

    result = service.sell("XYZ", 40)

    assert result.status == "done"
    assert _held(fake_broker) == 60
    [stop] = _open_sells(fake_broker)
    assert (stop.qty, stop.stop_price) == (60, Decimal(90))
