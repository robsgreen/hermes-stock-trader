import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.journal import record_entry
from trader_broker.models import BracketOrderRequest, Order, Proposal, SizedOrder
from trader_broker.service import TraderService, new_client_order_id
from trader_broker.state import StateStore

TODAY = date(2026, 11, 3)


def _sizing(qty: int = 100, entry: int = 100, stop: int = 90, equity: int = 100_000) -> SizedOrder:
    notional = Decimal(qty * entry)
    risk = Decimal(qty * (entry - stop))
    return SizedOrder(
        symbol="XYZ",
        qty=qty,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(120),
        notional=notional,
        pct_equity=float(notional / equity),
        risk_usd=risk,
        pct_equity_risk=float(risk / equity),
        bound_by="risk_size",
    )


def _place_entry(
    broker: FakeBroker,
    state: StateStore,
    now: datetime,
    *,
    age: timedelta = timedelta(0),
    sizing: SizedOrder | None = None,
) -> Order:
    """Place a trader-broker bracket buy for XYZ `age` ago, recorded in state first."""
    sizing = sizing or _sizing()
    client_order_id = new_client_order_id()
    state.record_order_intent(client_order_id, "XYZ", "buy", "entry", now - age, sizing=sizing)
    broker.set_now(now - age)
    entry = broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=sizing.qty,
            limit_price=sizing.entry_price,
            stop_price=sizing.stop_price,
            take_profit_price=sizing.target_price,
            client_order_id=client_order_id,
        )
    )
    state.update_order(client_order_id, entry.status, entry.id)
    broker.set_now(now)
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


def _order(broker: FakeBroker, order_id: str) -> Order:
    return next(order for order in broker.get_orders("all") if order.id == order_id)


def _open_stops(broker: FakeBroker) -> list[Order]:
    return [order for order in broker.get_orders("open") if order.type == "stop"]


# Stale entries


def test_entry_past_the_ttl_is_cancelled_and_a_younger_one_kept(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    stale = _place_entry(fake_broker, state_store, now, age=timedelta(minutes=31))
    fresh = _place_entry(fake_broker, state_store, now, age=timedelta(minutes=29))

    trader_service.housekeeping()

    assert _order(fake_broker, stale.id).status == "canceled"
    assert _order(fake_broker, fresh.id).status == "new"
    stored = state_store.order(stale.client_order_id)
    assert stored is not None
    assert stored.status == "canceled"


def test_partly_filled_entry_past_the_ttl_keeps_legs_for_its_filled_shares(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry = _place_entry(fake_broker, state_store, now, age=timedelta(minutes=31))
    fake_broker.fill_order(entry.id, qty=40)
    _journal(state_store, now, stop=Decimal(90))

    result = trader_service.housekeeping()

    assert _order(fake_broker, entry.id).status == "canceled"
    legs = [order for order in fake_broker.get_orders("open") if order.parent_id == entry.id]
    assert {leg.type for leg in legs} == {"limit", "stop"}
    assert result.protection_repairs == []  # the stop leg covers the 40 filled shares


# Unprotected positions


def test_unprotected_position_gets_a_gtc_stop_at_the_journal_stop(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    journal_id = _journal(state_store, now, stop=Decimal("91.50"))
    fake_broker.set_position("XYZ", 100, Decimal(100))

    result = trader_service.housekeeping()

    [stop] = _open_stops(fake_broker)
    assert (stop.qty, stop.stop_price, stop.time_in_force) == (100, Decimal("91.50"), "gtc")
    assert stop.client_order_id.startswith("tb-")
    stored = state_store.order(stop.client_order_id)
    assert stored is not None
    assert (stored.role, stored.broker_order_id, stored.journal_entry_id) == (
        "repair",
        stop.id,
        journal_id,
    )
    [unprotected] = result.protection_repairs
    assert (unprotected.symbol, unprotected.uncovered_qty, unprotected.action) == (
        "XYZ",
        100,
        "stop_placed",
    )


def test_position_without_a_journal_stop_gets_a_stop_8_percent_below_average_entry(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    fake_broker.set_position("XYZ", 50, Decimal("100.10"))

    trader_service.housekeeping()

    [stop] = _open_stops(fake_broker)
    assert (stop.qty, stop.stop_price) == (50, Decimal("92.09"))


def test_only_the_uncovered_part_of_a_position_gets_a_stop(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))
    fake_broker.submit_stop("XYZ", 60, Decimal(95), "manual-stop")

    trader_service.housekeeping()

    assert sorted(stop.qty for stop in _open_stops(fake_broker)) == [40, 60]


def test_bid_at_or_below_the_recorded_stop_sells_the_uncovered_shares_and_logs_it(
    trader_service: TraderService,
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _journal(state_store, now, stop=Decimal(90))
    fake_broker.set_position("XYZ", 100, Decimal(100), current_price=Decimal(89))

    with caplog.at_level(logging.WARNING, logger="trader_broker.service"):
        result = trader_service.housekeeping()

    assert fake_broker.get_positions() == []
    assert _open_stops(fake_broker) == []
    [unprotected] = result.protection_repairs
    assert unprotected.action == "sold"
    assert unprotected.client_order_id is not None
    stored = state_store.order(unprotected.client_order_id)
    assert stored is not None
    assert (stored.role, stored.status) == ("sell", "filled")
    assert "sold 100 unprotected XYZ at market" in caplog.text


def test_repair_stop_is_recorded_in_state_before_it_is_submitted(
    state_store: StateStore, now: datetime
) -> None:
    recorded_at_submit: list[bool] = []

    class RecordingBroker(FakeBroker):
        def submit_stop(
            self, symbol: str, qty: int, stop_price: Decimal, client_order_id: str
        ) -> Order:
            recorded_at_submit.append(state_store.order(client_order_id) is not None)
            return super().submit_stop(symbol, qty, stop_price, client_order_id)

    broker = RecordingBroker(now)
    broker.set_position("XYZ", 100, Decimal(100), current_price=Decimal(100))
    broker.set_account(cash=Decimal(90_000))
    service = TraderService(broker, state_store, Settings(), [], now=lambda: now)

    service.housekeeping()

    assert recorded_at_submit == [True]


def test_a_refused_repair_is_reported_and_the_pass_goes_on(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))
    fake_broker.fail_next("submit_stop")

    [unprotected] = trader_service.housekeeping().protection_repairs

    assert unprotected.action == "failed"
    assert unprotected.error == "scripted failure in submit_stop"


def test_no_bid_places_the_stop_instead_of_selling(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    # The IEX feed has no quote for the symbol: Alpaca reports a bid of 0.
    _journal(state_store, now, stop=Decimal(90))
    fake_broker.set_position("XYZ", 100, Decimal(100))
    fake_broker.set_quote("XYZ", bid=Decimal(0), ask=Decimal(0))

    [repair] = trader_service.housekeeping().protection_repairs

    assert repair.action == "stop_placed"
    assert [stop.qty for stop in _open_stops(fake_broker)] == [100]
    assert fake_broker.get_positions()[0].qty == 100


def test_partly_filled_open_entry_gets_a_repair_stop_for_its_filled_shares(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    # The broker holds a partial entry's legs inactive until the entry fills completely.
    entry = _place_entry(fake_broker, state_store, now)
    fake_broker.fill_order(entry.id, qty=40)
    _journal(state_store, now, stop=Decimal(90))

    [repair] = trader_service.housekeeping().protection_repairs

    assert (repair.uncovered_qty, repair.action, repair.stop_price) == (
        40,
        "stop_placed",
        Decimal(90),
    )
    [stop] = [order for order in _open_stops(fake_broker) if order.parent_id is None]
    assert stop.qty == 40


def test_repair_stop_is_cancelled_once_the_bracket_legs_cover_the_shares(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry = _place_entry(fake_broker, state_store, now)
    fake_broker.fill_order(entry.id, qty=40)
    _journal(state_store, now, stop=Decimal(90))
    trader_service.housekeeping()
    fake_broker.fill_order(entry.id)  # the legs now cover all 100 shares

    assert trader_service.housekeeping().protection_repairs == []

    [stop] = _open_stops(fake_broker)
    assert (stop.parent_id, stop.qty) == (entry.id, 100)


def test_repair_stop_is_cancelled_once_a_bracket_leg_has_sold_the_whole_position(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry = _place_entry(fake_broker, state_store, now)
    fake_broker.fill_order(entry.id, qty=40)
    _journal(state_store, now, stop=Decimal(90))
    trader_service.housekeeping()  # a repair stop for the 40 filled shares
    fake_broker.fill_order(entry.id)  # the legs now cover all 100 shares
    [stop_leg] = [order for order in _open_stops(fake_broker) if order.parent_id == entry.id]
    fake_broker.fill_order(stop_leg.id)  # the stop leg sells every share

    trader_service.housekeeping()

    assert fake_broker.get_positions() == []
    assert [order for order in fake_broker.get_orders("open") if order.side == "sell"] == []


def test_take_profit_fills_count_against_the_stop_legs_cover(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    fake_broker.set_position("XYZ", 40, Decimal(100))
    standalone = fake_broker.submit_stop("XYZ", 40, Decimal(90), new_client_order_id())
    entry = _place_entry(fake_broker, state_store, now)
    fake_broker.fill_order(entry.id)  # 140 held: 40 by the standalone stop, 100 by the legs
    trader_service.housekeeping()
    [take_profit] = [
        order
        for order in fake_broker.get_orders("open")
        if order.parent_id == entry.id and order.type == "limit"
    ]
    fake_broker.fill_order(take_profit.id, qty=40)  # 100 held; the stop leg covers 60

    assert trader_service.housekeeping().protection_repairs == []

    # The 100 shares held are covered by the standalone stop's 40 and the stop leg's 60.
    assert _order(fake_broker, standalone.id).status == "new"


def test_a_stop_larger_than_the_excess_cover_is_kept(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    fake_broker.set_position("XYZ", 20, Decimal(100))
    entry = _place_entry(fake_broker, state_store, now)
    fake_broker.fill_order(entry.id, qty=30)
    trader_service.housekeeping()  # a repair stop for all 50 shares held
    [repair_stop] = [order for order in _open_stops(fake_broker) if order.parent_id is None]
    fake_broker.fill_order(entry.id)  # 120 held; legs 100 + repair 50 cover 30 too many

    trader_service.housekeeping()

    # Cancelling the 50-share stop would leave 20 shares with no stop at all.
    assert _order(fake_broker, repair_stop.id).status == "new"


def test_stop_being_cancelled_does_not_count_as_cover(
    trader_service: TraderService, fake_broker: FakeBroker
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))
    manual = fake_broker.submit_stop("XYZ", 100, Decimal(95), "manual-stop")
    fake_broker.delay_cancels(reads=50)
    fake_broker.cancel_order(manual.id)

    [repair] = trader_service.housekeeping().protection_repairs

    # The broker still reserves the shares for it, so the repair is refused for now.
    assert (repair.uncovered_qty, repair.action) == (100, "failed")


# Entries whose submit outcome is unknown


def _unknown_entry(
    broker: FakeBroker, state: StateStore, now: datetime, *, placed: bool
) -> tuple[str, int]:
    """Record an entry intent with its proposal and review, as `propose_buy` does, and
    submit it only when `placed`; return its client order id and review id."""
    sizing = _sizing(qty=10)
    review_id = state.add_review(now, "hash", [], "proceed", sizing)
    proposal = Proposal(
        symbol="XYZ",
        stop_price=Decimal(90),
        target_price=Decimal(120),
        thesis="breakout",
        invalidation="close below 95",
        horizon_days=10,
    )
    client_order_id = new_client_order_id()
    state.record_order_intent(
        client_order_id,
        "XYZ",
        "buy",
        "entry",
        now,
        sizing=sizing,
        proposal=proposal,
        review_id=review_id,
    )
    if placed:
        broker.submit_bracket_buy(
            BracketOrderRequest("XYZ", 10, Decimal(100), Decimal(90), Decimal(120), client_order_id)
        )
    return client_order_id, review_id


def test_unknown_entry_the_broker_has_is_adopted_and_journaled(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    client_order_id, review_id = _unknown_entry(fake_broker, state_store, now, placed=True)

    trader_service.housekeeping()

    stored = state_store.order(client_order_id)
    assert stored is not None and stored.status == "new"
    [entry] = state_store.journal_entries("open")
    assert (entry.client_order_id, entry.review_id, entry.qty, entry.thesis) == (
        client_order_id,
        review_id,
        10,
        "breakout",
    )
    assert state_store.unresolved_entries() == []


def test_unknown_entry_already_journaled_is_not_journaled_again(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    # The buy was journaled, but storing its broker status failed, so it still reads
    # as unresolved.
    client_order_id, _ = _unknown_entry(fake_broker, state_store, now, placed=True)
    stored = state_store.order(client_order_id)
    assert stored is not None and stored.proposal is not None and stored.sizing is not None
    record_entry(
        state_store, stored.proposal, stored.sizing, client_order_id, stored.review_id, now
    )

    trader_service.housekeeping()

    assert [entry.client_order_id for entry in state_store.journal_entries()] == [client_order_id]
    assert state_store.unresolved_entries() == []


def test_unknown_entry_the_broker_lacks_is_closed_only_after_the_grace_period(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    client_order_id, _ = _unknown_entry(fake_broker, state_store, now, placed=False)
    clock = [now + timedelta(minutes=4)]
    service = TraderService(fake_broker, state_store, Settings(), [], now=lambda: clock[0])

    service.housekeeping()
    assert [stored.client_order_id for stored in state_store.unresolved_entries()] == [
        client_order_id
    ]

    clock[0] = now + timedelta(minutes=5)
    service.housekeeping()

    stored = state_store.order(client_order_id)
    assert stored is not None and stored.status == "rejected"
    assert state_store.journal_entries() == []


# Reconciliation and integrity violations


def test_price_drift_past_the_symbol_cap_is_not_a_violation(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry = _place_entry(fake_broker, state_store, now)
    fake_broker.fill_order(entry.id)
    _journal(state_store, now, stop=Decimal(90))
    fake_broker.set_price("XYZ", Decimal(300))  # 100 x 300 = 30k of 120k equity: 25%

    status = trader_service.get_risk_status()

    assert status.symbol_pct == {"XYZ": 0.25}
    assert status.protection_repairs == []
    assert state_store.integrity_violation_count() == 0
    stored = state_store.order(entry.client_order_id)
    assert stored is not None
    assert (stored.status, stored.broker_order_id) == ("filled", entry.id)


def test_entry_sized_over_a_cap_at_placement_is_a_violation(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _place_entry(fake_broker, state_store, now, sizing=_sizing(qty=250))  # 25% of equity

    trader_service.housekeeping()
    trader_service.housekeeping()

    assert state_store.integrity_violation_count() == 1


def test_foreign_order_is_one_violation_after_two_passes(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=10,
            limit_price=Decimal(95),
            stop_price=Decimal(90),
            take_profit_price=Decimal(110),
            client_order_id="manual-buy",
        )
    )

    trader_service.housekeeping()
    trader_service.housekeeping()

    assert state_store.integrity_violation_count() == 1


def test_position_trader_broker_did_not_buy_is_a_violation(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))

    trader_service.housekeeping()

    assert state_store.integrity_violation_count() == 1


def test_negative_cash_is_a_violation(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _journal(state_store, now, stop=Decimal(90))
    fake_broker.set_position("XYZ", 100, Decimal(100))
    fake_broker.set_account(cash=Decimal(-1_000), last_equity=Decimal(9_000))

    trader_service.housekeeping()

    assert state_store.integrity_violation_count() == 1


# Equity refresh and halts


def test_equity_refresh_raises_the_stored_peak(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_account(cash=Decimal(110_000), last_equity=Decimal(108_000))

    trader_service.refresh_equity()

    assert state_store.halts().peak == Decimal(110_000)


def test_daily_halt_stays_set_when_equity_recovers_the_same_day(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_account(cash=Decimal(95_900), last_equity=Decimal(100_000))  # -4.1%
    trader_service.refresh_equity()

    fake_broker.set_account(cash=Decimal(97_000), last_equity=Decimal(100_000))  # -3%
    equity = trader_service.refresh_equity()

    assert equity.halts is not None
    assert equity.halts.failures == ["daily_halt"]
    assert state_store.halts().daily_halt_date == TODAY


def test_kill_switch_stays_set_when_equity_recovers(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_account(cash=Decimal(79_000))  # 21% below the 100k peak
    trader_service.refresh_equity()

    fake_broker.set_account(cash=Decimal(95_000))
    equity = trader_service.refresh_equity()

    assert equity.halts is not None
    assert equity.halts.kill_switch
    assert state_store.halts().kill_switch


# State unavailable


def test_without_state_stops_are_still_placed_and_nothing_raises(
    fake_broker: FakeBroker, now: datetime
) -> None:
    stale = _place_entry_without_state(fake_broker, now)
    fake_broker.set_position("XYZ", 100, Decimal(100))
    service = TraderService(fake_broker, None, Settings(), [], now=lambda: now)

    status = service.get_risk_status()

    assert _order(fake_broker, stale.id).status == "canceled"
    [stop] = _open_stops(fake_broker)
    assert (stop.qty, stop.stop_price) == (100, Decimal(92))
    assert not status.state_available
    assert (status.peak, status.kill_switch, status.daily_halt) == (None, None, None)
    assert status.recent_blocks == []


def test_state_lost_while_open_does_not_stop_housekeeping(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_position("XYZ", 100, Decimal(100))
    state_store.close()  # every later state call fails

    result = trader_service.housekeeping()

    assert result.reading.halts is None
    assert [stop.qty for stop in _open_stops(fake_broker)] == [100]


def _place_entry_without_state(broker: FakeBroker, now: datetime) -> Order:
    broker.set_now(now - timedelta(minutes=45))
    entry = broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=10,
            limit_price=Decimal(95),
            stop_price=Decimal(90),
            take_profit_price=Decimal(110),
            client_order_id=new_client_order_id(),
        )
    )
    broker.set_now(now)
    return entry


# Risk status


def test_risk_status_reports_equity_halts_exposure_and_open_entries(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _journal(state_store, now, stop=Decimal(90))
    fake_broker.set_position("XYZ", 100, Decimal(100), current_price=Decimal(100))
    fake_broker.set_account(cash=Decimal(80_000), last_equity=Decimal(92_000))  # equity 90k
    entry = _place_entry(fake_broker, state_store, now, sizing=_sizing(qty=10))
    state_store.add_block(now, "propose_buy", "ABC", ["min_adv"], ["min_adv: too thin"])

    status = trader_service.get_risk_status()

    assert status.mode == "paper"
    assert status.state_available
    assert (status.equity, status.last_equity, status.cash) == (
        Decimal(90_000),
        Decimal(92_000),
        Decimal(80_000),
    )
    assert status.day_pnl_pct == pytest.approx(-2_000 / 92_000)
    assert status.peak == Decimal(100_000)
    assert status.drawdown_pct == pytest.approx(0.10)
    assert (status.kill_switch, status.daily_halt, status.halt_reasons) == (False, False, [])
    assert status.exposure_pct == pytest.approx(10_000 / 90_000)
    assert status.symbol_pct == {"XYZ": pytest.approx(10_000 / 90_000)}
    assert [order.id for order in status.open_entry_orders] == [entry.id]
    assert [block.symbol for block in status.recent_blocks] == ["ABC"]
    [unprotected] = status.protection_repairs
    assert unprotected.stop_price == Decimal(90)
