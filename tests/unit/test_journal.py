from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.models import Bar, BracketOrderRequest, Order
from trader_broker.service import TraderService, new_client_order_id
from trader_broker.state import StateStore

_NEW_YORK = ZoneInfo("America/New_York")

ENTRY_AT = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
"""Tuesday 2026-10-27, 11:00 New York time: when the test trades are bought."""
EXIT_AT = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
"""Monday 2026-11-02, 13:00 New York time: the session before the fixture's `now`."""


def _spy_closes(broker: FakeBroker, closes: dict[date, int]) -> None:
    """Give SPY one daily bar per date in `closes`, closing at the given price.

    Each bar is stamped at midnight New York time, as Alpaca stamps daily bars.
    """
    broker.set_daily_bars(
        "SPY",
        [
            Bar(
                timestamp=datetime(day.year, day.month, day.day, tzinfo=_NEW_YORK),
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=1_000_000,
            )
            for day, close in sorted(closes.items())
        ],
    )


def _buy(
    broker: FakeBroker,
    state: StateStore,
    *,
    stop: int = 90,
    target: int = 110,
    filled: bool = True,
    at: datetime = ENTRY_AT,
) -> tuple[Order, int]:
    """Buy 100 XYZ at 100 in a bracket at `at`, journaled; return the entry and its id."""
    client_order_id = new_client_order_id()
    broker.set_now(at)
    entry = broker.submit_bracket_buy(
        BracketOrderRequest(
            symbol="XYZ",
            qty=100,
            limit_price=Decimal(100),
            stop_price=Decimal(stop),
            take_profit_price=Decimal(target),
            client_order_id=client_order_id,
        )
    )
    if filled:
        broker.fill_order(entry.id)
    journal_id = state.add_journal_entry(
        symbol="XYZ",
        client_order_id=client_order_id,
        opened_at=at,
        qty=100,
        entry_price=Decimal(100),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        thesis="breakout",
        invalidation="close below 95",
        horizon_days=10,
    ).id
    return entry, journal_id


def _leg(broker: FakeBroker, entry: Order, order_type: str) -> Order:
    return next(
        order
        for order in broker.get_orders("all")
        if order.parent_id == entry.id and order.type == order_type
    )


@pytest.fixture
def spy_up_2pct(fake_broker: FakeBroker) -> None:
    """SPY closes at 500 on the entry date and 510 (+2%) on the exit date."""
    _spy_closes(
        fake_broker,
        {
            date(2026, 10, 26): 495,
            date(2026, 10, 27): 500,
            date(2026, 10, 28): 503,
            date(2026, 10, 29): 506,
            date(2026, 10, 30): 508,
            date(2026, 11, 2): 510,
        },
    )


# Close detection and settlement


@pytest.mark.usefixtures("spy_up_2pct")
def test_trade_up_10pct_while_spy_up_2pct_settles_with_8pct_alpha(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, journal_id = _buy(fake_broker, state_store)
    fake_broker.set_now(EXIT_AT)
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id)  # take profit at 110
    fake_broker.set_now(now)

    trader_service.housekeeping()

    settled = state_store.journal_entry(journal_id)
    assert settled is not None
    assert settled.status == "closed"
    assert settled.closed_at == EXIT_AT
    assert settled.exit_price == Decimal(110)
    assert settled.trade_return_pct == pytest.approx(0.10)
    assert settled.spy_return_pct == pytest.approx(0.02)
    assert settled.alpha_pct == pytest.approx(0.08)
    assert settled.lesson_pending is True


@pytest.mark.usefixtures("spy_up_2pct")
def test_settlement_measures_from_the_entry_fill_not_the_limit(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, journal_id = _buy(fake_broker, state_store, filled=False)
    fake_broker.fill_order(entry.id, qty=60, price=Decimal(98))  # under the 100 limit
    fake_broker.cancel_order(entry.id)  # the rest expires; the legs cover the 60 bought
    fake_broker.set_now(EXIT_AT)
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id)  # take profit at 110
    fake_broker.set_now(now)

    trader_service.housekeeping()

    settled = state_store.journal_entry(journal_id)
    assert settled is not None
    assert (settled.status, settled.qty, settled.entry_price) == ("closed", 60, Decimal(98))
    assert settled.trade_return_pct == pytest.approx(110 / 98 - 1)
    assert settled.alpha_pct == pytest.approx(110 / 98 - 1 - 0.02)


@pytest.mark.usefixtures("spy_up_2pct")
def test_partial_sell_leaves_the_entry_open(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, journal_id = _buy(fake_broker, state_store)
    fake_broker.set_now(EXIT_AT)
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id, qty=40)
    fake_broker.set_now(now)

    trader_service.housekeeping()

    still_open = state_store.journal_entry(journal_id)
    assert still_open is not None
    assert (still_open.status, still_open.exit_price, still_open.lesson_pending) == (
        "open",
        None,
        False,
    )


@pytest.mark.usefixtures("spy_up_2pct")
def test_stop_out_at_a_gap_price_settles_the_actual_loss(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _, journal_id = _buy(fake_broker, state_store, stop=90)
    fake_broker.set_now(EXIT_AT)
    fake_broker.trigger_stop("XYZ", price=Decimal(80))  # gapped through the 90 stop
    fake_broker.set_now(now)

    trader_service.housekeeping()

    settled = state_store.journal_entry(journal_id)
    assert settled is not None
    assert settled.exit_price == Decimal(80)
    assert settled.trade_return_pct == pytest.approx(-0.20)
    assert settled.alpha_pct == pytest.approx(-0.22)
    assert settled.lesson_pending is True


@pytest.mark.usefixtures("spy_up_2pct")
def test_exit_price_is_the_qty_weighted_average_of_the_sell_fills(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, journal_id = _buy(fake_broker, state_store, target=105)
    fake_broker.set_now(EXIT_AT - timedelta(hours=1))
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id, qty=40)  # 40 at 105
    fake_broker.set_now(EXIT_AT)
    fake_broker.trigger_stop("XYZ")  # the other 60 at the 90 stop
    fake_broker.set_now(now)

    trader_service.housekeeping()

    settled = state_store.journal_entry(journal_id)
    assert settled is not None
    assert settled.exit_price == Decimal(96)  # (40 * 105 + 60 * 90) / 100
    assert settled.closed_at == EXIT_AT


def test_trade_closed_today_settles_once_spys_close_is_published(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    clock = [now]
    service = TraderService(
        fake_broker, state_store, Settings(), reviewers=[], now=lambda: clock[0]
    )
    spy = {date(2026, 10, 27): 500, date(2026, 11, 2): 505}
    _spy_closes(fake_broker, spy)
    entry, journal_id = _buy(fake_broker, state_store)
    fake_broker.set_now(now)
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id)

    service.housekeeping()

    closed = state_store.journal_entry(journal_id)
    assert closed is not None
    assert (closed.status, closed.exit_price, closed.spy_return_pct, closed.lesson_pending) == (
        "closed",
        Decimal(110),
        None,
        False,
    )

    spy[date(2026, 11, 3)] = 510
    _spy_closes(fake_broker, spy)
    clock[0] = now + timedelta(days=1)
    service.housekeeping()

    settled = state_store.journal_entry(journal_id)
    assert settled is not None
    assert settled.spy_return_pct == pytest.approx(0.02)  # 500 on entry, 510 on exit day
    assert settled.alpha_pct == pytest.approx(0.08)
    assert settled.lesson_pending is True


@pytest.mark.usefixtures("spy_up_2pct")
def test_entry_waiting_to_fill_stays_open(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    _, journal_id = _buy(fake_broker, state_store, filled=False, at=now)

    trader_service.housekeeping()

    waiting = state_store.journal_entry(journal_id)
    assert waiting is not None
    assert waiting.status == "open"


@pytest.mark.usefixtures("spy_up_2pct")
def test_entry_that_expired_unfilled_closes_without_settling(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, journal_id = _buy(fake_broker, state_store, filled=False)
    fake_broker.cancel_order(entry.id)
    fake_broker.set_now(now)

    trader_service.housekeeping()

    closed = state_store.journal_entry(journal_id)
    assert closed is not None
    assert (closed.status, closed.closed_at, closed.exit_price, closed.lesson_pending) == (
        "closed",
        now,
        None,
        False,
    )
    assert trader_service.get_journal(status="pending_lessons") == []


@pytest.mark.usefixtures("spy_up_2pct")
def test_unreadable_fills_leave_the_entry_open_and_housekeeping_finishes(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, journal_id = _buy(fake_broker, state_store)
    fake_broker.set_now(EXIT_AT)
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id)
    fake_broker.set_now(now)
    fake_broker.fail_next("get_fills")

    trader_service.housekeeping()
    still_open = state_store.journal_entry(journal_id)
    trader_service.housekeeping()
    settled = state_store.journal_entry(journal_id)

    assert still_open is not None and still_open.status == "open"
    assert settled is not None and settled.lesson_pending is True


# Journal tools


@pytest.mark.usefixtures("spy_up_2pct")
def test_pending_lessons_lists_only_settled_trades_and_a_note_clears_the_flag(
    trader_service: TraderService, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    entry, settled_id = _buy(fake_broker, state_store)
    fake_broker.set_now(EXIT_AT)
    fake_broker.fill_order(_leg(fake_broker, entry, "limit").id)
    fake_broker.set_now(now)
    _, open_id = _buy(fake_broker, state_store, filled=False, at=now)
    trader_service.housekeeping()

    assert [e.id for e in trader_service.get_journal(status="pending_lessons")] == [settled_id]
    assert [e.id for e in trader_service.get_journal(status="open")] == [open_id]
    assert [e.id for e in trader_service.get_journal(status="closed")] == [settled_id]
    assert [e.id for e in trader_service.get_journal()] == [open_id, settled_id]
    assert [e.id for e in trader_service.get_journal(limit=1)] == [open_id]

    noted = trader_service.journal_note(settled_id, "Took profit at target; SPY lagged.")

    assert noted.lesson == "Took profit at target; SPY lagged."
    assert noted.lesson_pending is False
    assert trader_service.get_journal(status="pending_lessons") == []


def test_journal_note_rejects_an_unknown_entry(trader_service: TraderService) -> None:
    with pytest.raises(KeyError, match="journal entry 999"):
        trader_service.journal_note(999, "no such trade")
