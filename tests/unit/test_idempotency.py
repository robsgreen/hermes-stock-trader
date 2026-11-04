"""propose_buy submits each bracket at most once, under a client id stored beforehand."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from trader_broker.brokers.base import UnknownResultError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.models import BracketOrderRequest, Order, Proposal, ReviewVerdict
from trader_broker.service import TraderService
from trader_broker.state import StateStore, StoredOrder

APPROVE = ReviewVerdict(verdict="approve", confidence="medium", reasons=["clean breakout"])
PROPOSAL = Proposal(
    symbol="XYZ",
    stop_price=Decimal(95),
    target_price=Decimal(120),
    thesis="Breakout above a six-month base on rising volume.",
    invalidation="A close back inside the base.",
    horizon_days=20,
    limit_price=Decimal(100),
)


class SubmitSpy:
    """Wraps `FakeBroker.submit_bracket_buy`, noting each call and what state held then."""

    def __init__(self, broker: FakeBroker, state: StateStore) -> None:
        self._submit = broker.submit_bracket_buy
        self._state = state
        self.requests: list[BracketOrderRequest] = []
        self.stored_at_submit: list[StoredOrder | None] = []

    def __call__(self, req: BracketOrderRequest) -> Order:
        self.requests.append(req)
        self.stored_at_submit.append(self._state.order(req.client_order_id))
        return self._submit(req)


@pytest.fixture
def submit_spy(
    fake_broker: FakeBroker, state_store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> SubmitSpy:
    spy = SubmitSpy(fake_broker, state_store)
    monkeypatch.setattr(fake_broker, "submit_bracket_buy", spy)
    return spy


@pytest.fixture
def service(
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    fake_reviewer: Callable[..., Any],
) -> TraderService:
    reviewers = [fake_reviewer("openai", APPROVE), fake_reviewer("xai", APPROVE)]
    return TraderService(fake_broker, state_store, Settings(), reviewers, now=lambda: now)


def _entries(broker: FakeBroker) -> list[Order]:
    return [order for order in broker.get_orders("all") if order.side == "buy"]


def test_accepted_submit_that_times_out_counts_as_placed_once(
    service: TraderService,
    fake_broker: FakeBroker,
    state_store: StateStore,
    submit_spy: SubmitSpy,
) -> None:
    fake_broker.accept_then_time_out("submit_bracket_buy")

    result = service.propose_buy(PROPOSAL)

    assert result.status == "placed"
    [entry] = _entries(fake_broker)
    assert len(submit_spy.requests) == 1
    assert result.order is not None
    assert (result.order.order_id, result.order.client_order_id) == (
        entry.id,
        entry.client_order_id,
    )
    stored = state_store.order(entry.client_order_id)
    assert stored is not None and stored.broker_order_id == entry.id
    assert len(state_store.journal_entries()) == 1


def test_submit_that_times_out_without_an_order_is_unknown_and_not_retried(
    service: TraderService,
    fake_broker: FakeBroker,
    state_store: StateStore,
    submit_spy: SubmitSpy,
) -> None:
    fake_broker.fail_next("submit_bracket_buy", UnknownResultError("read timed out"))

    result = service.propose_buy(PROPOSAL)

    assert result.status == "unknown"
    [request] = submit_spy.requests
    assert request.client_order_id in result.reasons[0]
    assert _entries(fake_broker) == []
    assert state_store.journal_entries() == []
    assert state_store.recent_blocks() == []
    [unresolved] = state_store.unresolved_entries()
    assert unresolved.client_order_id == request.client_order_id
    assert unresolved.proposal == PROPOSAL


def test_a_failed_lookup_after_a_lost_reply_is_unknown_too(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.accept_then_time_out("submit_bracket_buy")
    fake_broker.fail_next("get_order_by_client_id")

    result = service.propose_buy(PROPOSAL)

    assert result.status == "unknown"
    assert len(state_store.unresolved_entries()) == 1


def test_a_buy_of_a_symbol_with_an_unknown_entry_is_blocked_until_it_is_resolved(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.fail_next("submit_bracket_buy", UnknownResultError("read timed out"))
    service.propose_buy(PROPOSAL)

    result = service.propose_buy(PROPOSAL)

    assert result.status == "blocked"
    assert [reason.split(":")[0] for reason in result.reasons] == ["pending_unknown"]
    [block] = state_store.recent_blocks()
    assert block.rules == ["pending_unknown"]


def test_a_lost_reply_whose_order_appears_later_is_journaled_by_housekeeping(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    # The broker accepted the order, but neither the reply nor the first lookup got back.
    fake_broker.accept_then_time_out("submit_bracket_buy")
    fake_broker.fail_next("get_order_by_client_id")
    service.propose_buy(PROPOSAL)
    [entry] = _entries(fake_broker)
    fake_broker.fill_order(entry.id)

    status = service.get_risk_status()

    [journaled] = state_store.journal_entries("open")
    assert journaled.client_order_id == entry.client_order_id
    assert status.protection_repairs == []
    assert state_store.integrity_violation_count() == 0


def test_a_refused_submit_is_stored_as_rejected(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore, submit_spy: SubmitSpy
) -> None:
    fake_broker.fail_next("submit_bracket_buy")

    result = service.propose_buy(PROPOSAL)

    assert result.status == "blocked"
    [request] = submit_spy.requests
    stored = state_store.order(request.client_order_id)
    assert stored is not None and stored.status == "rejected"
    assert state_store.unresolved_entries() == []


def test_client_id_and_sizing_are_stored_before_the_submit(
    service: TraderService, submit_spy: SubmitSpy
) -> None:
    service.propose_buy(PROPOSAL)

    [request] = submit_spy.requests
    [stored] = submit_spy.stored_at_submit
    assert request.client_order_id.startswith("tb-")
    assert stored is not None
    assert (stored.role, stored.side, stored.broker_order_id) == ("entry", "buy", None)
    assert stored.sizing is not None and stored.sizing.qty == request.qty
