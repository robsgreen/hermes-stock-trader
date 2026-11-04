import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader_broker.models import Proposal, ReviewOutcome, ReviewVerdict, SizedOrder
from trader_broker.state import (
    Block,
    HaltState,
    StateStore,
    StateUnavailableError,
    StoredReview,
    init_state,
)

SIZED = SizedOrder(
    symbol="XYZ",
    qty=200,
    entry_price=Decimal("100.50"),
    stop_price=Decimal(90),
    target_price=Decimal(120),
    notional=Decimal(20_100),
    pct_equity=0.201,
    risk_usd=Decimal(2_100),
    pct_equity_risk=0.021,
    bound_by="symbol_cap",
)


def test_opening_a_missing_file_is_unavailable_and_creates_nothing(
    missing_state_path: Path,
) -> None:
    with pytest.raises(StateUnavailableError):
        StateStore.open(missing_state_path)

    assert not missing_state_path.exists()
    assert not missing_state_path.parent.exists()


def test_opening_a_missing_file_in_an_existing_directory_creates_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(StateUnavailableError):
        StateStore.open(tmp_path / "state.db")

    assert list(tmp_path.iterdir()) == []


def test_opening_a_corrupt_file_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite database" * 100)

    with pytest.raises(StateUnavailableError, match="unreadable"):
        StateStore.open(path)


def test_opening_an_empty_file_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.touch()

    with pytest.raises(StateUnavailableError, match="not a trader-broker state database"):
        StateStore.open(path)


def test_opening_another_sqlite_database_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
    connection.close()

    with pytest.raises(StateUnavailableError, match="not a trader-broker state database"):
        StateStore.open(path)


def test_opening_another_schema_version_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    init_state(path, Decimal(100_000)).close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE account_state SET schema_version = 99")
    connection.close()

    with pytest.raises(StateUnavailableError, match="schema version 99"):
        StateStore.open(path)


def test_init_state_starts_the_peak_and_paper_period(state_store: StateStore) -> None:
    assert state_store.halts() == HaltState(
        peak=Decimal(100_000), kill_switch=False, daily_halt_date=None
    )
    assert state_store.paper_start_date() == date(2026, 11, 3)


def test_init_state_refuses_an_existing_database(state_store: StateStore) -> None:
    path = state_store.path

    with pytest.raises(FileExistsError, match="--force"):
        init_state(path, Decimal(50_000))

    assert state_store.halts().peak == Decimal(100_000)


def test_init_state_with_force_replaces_an_existing_database(state_store: StateStore) -> None:
    path = state_store.path
    state_store.update_halts(lambda _: HaltState(Decimal(120_000), True, date(2026, 11, 3)))
    state_store.close()

    with init_state(path, Decimal(50_000), force=True, today=date(2026, 11, 4)) as store:
        assert store.halts() == HaltState(
            peak=Decimal(50_000), kill_switch=False, daily_halt_date=None
        )
        assert store.paper_start_date() == date(2026, 11, 4)


def test_init_state_with_force_replaces_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"garbage")

    with init_state(path, Decimal(100_000), force=True) as store:
        assert store.halts().peak == Decimal(100_000)


def test_halts_survive_close_and_reopen(state_store: StateStore) -> None:
    path = state_store.path
    state_store.update_halts(lambda _: HaltState(Decimal("123456.78"), True, date(2026, 11, 3)))
    state_store.close()

    with StateStore.open(path) as reopened:
        assert reopened.halts() == HaltState(
            peak=Decimal("123456.78"), kill_switch=True, daily_halt_date=date(2026, 11, 3)
        )


def test_reset_kill_switch_clears_it_and_restarts_the_peak(state_store: StateStore) -> None:
    state_store.update_halts(lambda _: HaltState(Decimal(120_000), True, date(2026, 11, 3)))

    state_store.reset_kill_switch(Decimal(95_900))

    assert state_store.halts() == HaltState(
        peak=Decimal(95_900), kill_switch=False, daily_halt_date=date(2026, 11, 3)
    )


def test_update_halts_stores_the_update_and_keeps_the_kill_switch_latched(
    state_store: StateStore,
) -> None:
    state_store.update_halts(lambda _: HaltState(Decimal(100_000), True, None))

    stored = state_store.update_halts(
        lambda halts: HaltState(
            halts.peak + 5, kill_switch=False, daily_halt_date=date(2026, 11, 3)
        )
    )

    assert stored == HaltState(
        peak=Decimal(100_005), kill_switch=True, daily_halt_date=date(2026, 11, 3)
    )
    assert state_store.halts() == stored


def test_update_halts_holds_the_write_lock_between_read_and_write(
    state_store: StateStore,
) -> None:
    def reset_from_another_process(halts: HaltState) -> HaltState:
        other = sqlite3.connect(state_store.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE account_state SET kill_switch = 0")
        finally:
            other.close()
        return halts

    state_store.update_halts(reset_from_another_process)


def test_a_failed_halt_update_stores_nothing(state_store: StateStore) -> None:
    def fail(halts: HaltState) -> HaltState:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        state_store.update_halts(fail)

    assert state_store.halts() == HaltState(Decimal(100_000), False, None)


def test_order_intent_is_recorded_before_its_status_update(
    state_store: StateStore, now: datetime
) -> None:
    state_store.record_order_intent("tb-1", "XYZ", "buy", "entry", now, sizing=SIZED)

    intent = state_store.order("tb-1")
    assert intent is not None
    assert (intent.broker_order_id, intent.status) == (None, None)
    assert intent.sizing == SIZED
    assert intent.created_at == now

    state_store.update_order("tb-1", "new", broker_order_id="broker-1")
    state_store.update_order("tb-1", "filled")

    placed = state_store.order("tb-1")
    assert placed is not None
    assert (placed.broker_order_id, placed.status, placed.role) == ("broker-1", "filled", "entry")


def test_entry_intent_keeps_what_journaling_it_needs_until_resolved(
    state_store: StateStore, now: datetime
) -> None:
    proposal = Proposal(
        symbol="XYZ",
        stop_price=Decimal(95),
        target_price=Decimal(120),
        thesis="Breakout.",
        invalidation="Back in the base.",
        horizon_days=10,
    )
    review_id = state_store.add_review(now, "h", [], "proceed", SIZED)
    state_store.record_order_intent(
        "tb-1", "XYZ", "buy", "entry", now, sizing=SIZED, proposal=proposal, review_id=review_id
    )
    state_store.record_order_intent("tb-2", "XYZ", "buy", "entry", now, sizing=SIZED)
    state_store.record_order_intent("tb-3", "XYZ", "sell", "sell", now)
    state_store.update_order("tb-2", "rejected")

    [unresolved] = state_store.unresolved_entries()

    assert unresolved.client_order_id == "tb-1"
    assert (unresolved.proposal, unresolved.review_id) == (proposal, review_id)


def test_order_lookup_of_an_unknown_client_id_is_none(state_store: StateStore) -> None:
    assert state_store.order("tb-unknown") is None


def test_recording_a_client_id_twice_is_rejected(state_store: StateStore, now: datetime) -> None:
    state_store.record_order_intent("tb-1", "XYZ", "buy", "entry", now, sizing=SIZED)

    with pytest.raises(ValueError, match="already recorded"):
        state_store.record_order_intent("tb-1", "XYZ", "sell", "sell", now)


def test_updating_an_unrecorded_order_is_rejected(state_store: StateStore) -> None:
    with pytest.raises(KeyError):
        state_store.update_order("tb-unknown", "new")


def test_journal_rows_link_to_their_review_and_orders(
    state_store: StateStore, now: datetime
) -> None:
    verdict = ReviewVerdict(verdict="approve", confidence="high", reasons=["clean breakout"])
    review_id = state_store.add_review(
        now,
        "abc123",
        [
            ReviewOutcome(reviewer="reviewer_1", verdict=verdict),
            ReviewOutcome(reviewer="reviewer_2", error="timeout"),
        ],
        "proceed",
        SIZED,
    )
    entry = state_store.add_journal_entry(
        symbol="XYZ",
        client_order_id="tb-1",
        opened_at=now,
        qty=200,
        entry_price=Decimal("100.50"),
        stop_price=Decimal(90),
        target_price=Decimal(120),
        thesis="Breakout above the 50-day high on volume.",
        invalidation="Close back below 95.",
        horizon_days=10,
        review_id=review_id,
    )
    state_store.record_order_intent("tb-2", "XYZ", "sell", "repair", now, journal_entry_id=entry.id)

    assert entry.review_id == review_id
    assert (entry.status, entry.entry_price, entry.opened_at) == ("open", Decimal("100.50"), now)
    assert state_store.journal_entry(entry.id) == entry
    stop = state_store.order("tb-2")
    assert stop is not None
    assert stop.journal_entry_id == entry.id


def test_journal_entries_are_listed_newest_first_and_filtered_by_status(
    state_store: StateStore, now: datetime
) -> None:
    for symbol in ("AAA", "BBB", "CCC"):
        state_store.add_journal_entry(
            symbol=symbol,
            client_order_id=f"tb-{symbol}",
            opened_at=now,
            qty=1,
            entry_price=Decimal(10),
            stop_price=Decimal(9),
            target_price=Decimal(12),
            thesis="t",
            invalidation="i",
            horizon_days=5,
        )

    assert [e.symbol for e in state_store.journal_entries()] == ["CCC", "BBB", "AAA"]
    assert [e.symbol for e in state_store.journal_entries(limit=2)] == ["CCC", "BBB"]
    assert len(state_store.journal_entries(status="open")) == 3
    assert state_store.journal_entries(status="closed") == []
    assert state_store.journal_entry(999) is None


def test_recent_blocks_are_listed_newest_first(state_store: StateStore, now: datetime) -> None:
    state_store.add_block(now, "propose_buy", "XYZ", ["min_price"], ["min_price: $4"])
    later = now + timedelta(minutes=5)
    state_store.add_block(
        later, "propose_buy", "ABC", ["cash", "review", "cash"], ["cash: a", "gpt: b", "cash: c"]
    )

    assert state_store.recent_blocks() == [
        Block(
            at=later,
            tool="propose_buy",
            symbol="ABC",
            rules=["cash", "review"],
            reasons=["cash: a", "gpt: b", "cash: c"],
        ),
        Block(
            at=now, tool="propose_buy", symbol="XYZ", rules=["min_price"], reasons=["min_price: $4"]
        ),
    ]
    assert len(state_store.recent_blocks(limit=1)) == 1


def test_blocks_and_reviews_since_a_time_are_listed_in_the_order_recorded(
    state_store: StateStore, now: datetime
) -> None:
    # Stored in UTC, asked for in New York time: the comparison is by instant, not text.
    since = now.astimezone(ZoneInfo("America/New_York"))
    before, later = now - timedelta(microseconds=1), now + timedelta(minutes=5)
    state_store.add_block(before, "propose_buy", "OLD", ["min_price"], ["min_price: x"])
    state_store.add_block(later, "propose_buy", "ABC", ["leverage"], ["leverage: x"])
    state_store.add_block(now, "propose_buy", "XYZ", ["cash"], ["cash: x"])
    reject = ReviewVerdict(verdict="reject", confidence="high", reasons=["extended"])
    outcomes = [ReviewOutcome(reviewer="reviewer_1", verdict=reject)]
    state_store.add_review(before, "old", outcomes, "block", None)
    blocked_id = state_store.add_review(now, "h1", outcomes, "block", None)
    proceeded_id = state_store.add_review(later, "h2", [], "proceed", SIZED)

    assert [block.symbol for block in state_store.blocks_since(since)] == ["ABC", "XYZ"]
    assert state_store.reviews_since(since) == [
        StoredReview(
            id=blocked_id,
            created_at=now,
            packet_hash="h1",
            outcomes=outcomes,
            decision="block",
            final_order=None,
        ),
        StoredReview(
            id=proceeded_id,
            created_at=later,
            packet_hash="h2",
            outcomes=[],
            decision="proceed",
            final_order=SIZED,
        ),
    ]


def test_the_same_integrity_violation_counts_once(state_store: StateStore, now: datetime) -> None:
    assert state_store.record_integrity_violation(now, "foreign_order", "o-1", "not ours")
    assert not state_store.record_integrity_violation(
        now + timedelta(minutes=1), "foreign_order", "o-1", "not ours"
    )
    assert state_store.record_integrity_violation(now, "foreign_order", "o-2", "not ours")

    assert state_store.integrity_violation_count() == 2


def test_a_sqlite_failure_while_open_reports_unavailable(
    state_store: StateStore, now: datetime
) -> None:
    with sqlite3.connect(state_store.path) as other:
        other.execute("DROP TABLE blocks")
    other.close()

    with pytest.raises(StateUnavailableError):
        state_store.add_block(now, "propose_buy", "XYZ", ["min_price"], ["min_price: x"])
