"""propose_buy: the risk gate, the cross-model review, the bracket and its records."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trader_broker.brokers.base import BrokerError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.models import Asset, Bar, Clock, Order, Proposal, ReviewOutcome, ReviewVerdict
from trader_broker.review import ReviewPacket
from trader_broker.service import BuyResult, TraderService
from trader_broker.state import HaltState, StateStore

APPROVE = ReviewVerdict(verdict="approve", confidence="medium", reasons=["clean breakout"])


def _proposal(**changes: Any) -> Proposal:
    """Buy XYZ with a 100 limit, stop 95 and target 120, unless `changes` say otherwise."""
    fields: dict[str, Any] = {
        "symbol": "XYZ",
        "stop_price": Decimal(95),
        "target_price": Decimal(120),
        "thesis": "Breakout above a six-month base on rising volume.",
        "invalidation": "A close back inside the base.",
        "horizon_days": 20,
        "limit_price": Decimal(100),
    }
    return Proposal(**(fields | changes))


def _service(
    broker: FakeBroker, state: StateStore | None, now: datetime, *reviewers: Any
) -> TraderService:
    return TraderService(broker, state, Settings(), reviewers=list(reviewers), now=lambda: now)


@pytest.fixture
def reviewers(fake_reviewer: Callable[..., Any]) -> list[Any]:
    """Two `FakeReviewer`s that approve, named like the default providers."""
    return [fake_reviewer("openai", APPROVE), fake_reviewer("xai", APPROVE)]


@pytest.fixture
def service(
    fake_broker: FakeBroker, state_store: StateStore, now: datetime, reviewers: list[Any]
) -> TraderService:
    """Over `fake_broker` and `state_store`, with two approving reviewers."""
    return _service(fake_broker, state_store, now, *reviewers)


def _buys(broker: FakeBroker) -> list[Order]:
    return [order for order in broker.get_orders("all") if order.side == "buy"]


def _review_row(state: StateStore, review_id: int) -> sqlite3.Row:
    with sqlite3.connect(state.path) as connection:
        connection.row_factory = sqlite3.Row
        row: sqlite3.Row = connection.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
    return row


def _assert_blocked_and_recorded(result: BuyResult, state: StateStore, rule: str) -> None:
    assert result.status == "blocked"
    assert result.order is None
    assert any(reason.startswith(f"{rule}: ") for reason in result.reasons), result.reasons
    [block] = state.recent_blocks()
    assert (block.tool, block.symbol, block.reasons) == ("propose_buy", "XYZ", result.reasons)


def test_approved_buy_places_one_gtc_bracket(
    service: TraderService, fake_broker: FakeBroker, reviewers: list[Any]
) -> None:
    result = service.propose_buy(_proposal())

    assert (result.status, result.reasons) == ("placed", [])
    assert result.order is not None
    assert (
        result.order.qty,
        result.order.limit_price,
        result.order.stop_price,
        result.order.target_price,
    ) == (200, Decimal(100), Decimal(95), Decimal(120))
    assert result.order.client_order_id.startswith("tb-")

    [entry] = _buys(fake_broker)
    assert (entry.id, entry.client_order_id) == (
        result.order.order_id,
        result.order.client_order_id,
    )
    assert (entry.type, entry.order_class, entry.time_in_force) == ("limit", "bracket", "gtc")
    assert (entry.qty, entry.limit_price) == (200, Decimal(100))
    legs = {leg.type: leg for leg in fake_broker.get_orders("all") if leg.parent_id == entry.id}
    assert (legs["limit"].limit_price, legs["limit"].time_in_force) == (Decimal(120), "gtc")
    assert (legs["stop"].stop_price, legs["stop"].time_in_force) == (Decimal(95), "gtc")
    assert [len(reviewer.packets) for reviewer in reviewers] == [1, 1]
    assert [(r.reviewer, r.verdict, r.confidence) for r in result.reviews] == [
        ("openai", "approve", "medium"),
        ("xai", "approve", "medium"),
    ]


def test_a_limit_far_above_a_cheap_quote_is_blocked_before_review(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore, reviewers: list[Any]
) -> None:
    # A $3 stock proposed with a $5 limit: the limit alone would pass min_price.
    fake_broker.set_quote("XYZ", bid=Decimal("2.99"), ask=Decimal(3))

    result = service.propose_buy(
        _proposal(limit_price=Decimal(5), stop_price=Decimal("4.70"), target_price=Decimal(6))
    )

    _assert_blocked_and_recorded(result, state_store, "limit_invalid")
    assert {reason.split(":")[0] for reason in result.reasons} == {
        "limit_invalid",
        "stop_invalid",
        "min_price",
    }
    [block] = state_store.recent_blocks()
    assert block.rules == ["limit_invalid", "stop_invalid", "min_price"]
    assert _buys(fake_broker) == []
    assert [len(reviewer.packets) for reviewer in reviewers] == [0, 0]


def test_buy_without_a_limit_enters_at_a_marketable_limit_above_the_ask(
    service: TraderService, fake_broker: FakeBroker
) -> None:
    result = service.propose_buy(_proposal(limit_price=None))

    # ask 100.02 * 1.005 = 100.5201 -> 100.52; the 20% symbol cap allows 198 shares.
    [entry] = _buys(fake_broker)
    assert (entry.limit_price, entry.qty) == (Decimal("100.52"), 198)
    assert result.order is not None and result.order.qty == 198


def test_placed_buy_is_journaled_and_linked_to_its_stored_review(
    service: TraderService,
    state_store: StateStore,
    now: datetime,
    reviewers: list[Any],
) -> None:
    proposal = _proposal()

    result = service.propose_buy(proposal)

    assert result.order is not None
    [journal_entry] = state_store.journal_entries("open")
    assert journal_entry.client_order_id == result.order.client_order_id
    assert (
        journal_entry.symbol,
        journal_entry.qty,
        journal_entry.entry_price,
        journal_entry.stop_price,
        journal_entry.target_price,
        journal_entry.opened_at,
    ) == ("XYZ", 200, Decimal(100), Decimal(95), Decimal(120), now)
    assert (journal_entry.thesis, journal_entry.invalidation, journal_entry.horizon_days) == (
        proposal.thesis,
        proposal.invalidation,
        proposal.horizon_days,
    )

    assert journal_entry.review_id is not None
    review = _review_row(state_store, journal_entry.review_id)
    assert review["packet_hash"] == reviewers[0].packets[0].sha256
    assert review["decision"] == "proceed"
    assert [outcome["reviewer"] for outcome in json.loads(review["outcomes"])] == ["openai", "xai"]
    assert json.loads(review["final_order"])["qty"] == 200

    stored = state_store.order(result.order.client_order_id)
    assert stored is not None
    assert (stored.role, stored.status, stored.broker_order_id) == (
        "entry",
        "new",
        result.order.order_id,
    )
    assert stored.sizing is not None and stored.sizing.qty == 200


def _closed_market(broker: FakeBroker, now: datetime) -> None:
    broker.set_clock(
        Clock(timestamp=now, is_open=False, next_open=now + timedelta(hours=1), next_close=now)
    )


def _near_close(broker: FakeBroker, now: datetime) -> None:
    broker.set_clock(
        Clock(
            timestamp=now,
            is_open=True,
            next_open=now + timedelta(days=1),
            next_close=now + timedelta(minutes=20),
        )
    )


def _inactive(broker: FakeBroker, now: datetime) -> None:
    broker.set_asset(
        Asset(
            symbol="XYZ",
            asset_class="us_equity",
            exchange="NASDAQ",
            status="inactive",
            tradable=False,
        )
    )


def _illiquid(broker: FakeBroker, now: datetime) -> None:
    thin = [bar.model_copy(update={"volume": 10_000}) for bar in broker.get_daily_bars("XYZ", 260)]
    broker.set_daily_bars("XYZ", thin)


def _no_cash_left(broker: FakeBroker, now: datetime) -> None:
    """All but $50 of the $100k is in ABC, so not one XYZ share is affordable."""
    broker.set_position("ABC", 1000, Decimal(100), current_price=Decimal("99.95"))
    broker.set_account(cash=Decimal(50), last_equity=Decimal(100_000))


def _unchanged(broker: FakeBroker, now: datetime) -> None:
    pass


@pytest.mark.parametrize(
    ("rule", "setup", "proposal"),
    [
        ("market_closed", _closed_market, _proposal()),
        ("too_late", _near_close, _proposal()),
        ("not_tradable", _inactive, _proposal()),
        ("min_adv", _illiquid, _proposal()),
        (
            "min_price",
            _unchanged,
            _proposal(
                limit_price=Decimal("4.99"),
                stop_price=Decimal("4.70"),
                target_price=Decimal(6),
            ),
        ),
        ("stop_invalid", _unchanged, _proposal(stop_price=Decimal(100))),
        ("target_invalid", _unchanged, _proposal(target_price=Decimal(100))),
        ("qty_zero", _no_cash_left, _proposal()),
    ],
)
def test_gate_failure_blocks_with_its_rule_and_records_the_block(
    rule: str,
    setup: Callable[[FakeBroker, datetime], None],
    proposal: Proposal,
    service: TraderService,
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    reviewers: list[Any],
) -> None:
    setup(fake_broker, now)

    result = service.propose_buy(proposal)

    _assert_blocked_and_recorded(result, state_store, rule)
    assert _buys(fake_broker) == []
    assert [reviewer.packets for reviewer in reviewers] == [[], []]
    assert result.reviews == []


def test_missing_state_blocks_buys_but_not_sells(
    fake_broker: FakeBroker, now: datetime, reviewers: list[Any]
) -> None:
    fake_broker.set_position("XYZ", 10, Decimal(100))
    service = _service(fake_broker, None, now, *reviewers)

    result = service.propose_buy(_proposal())

    assert result.status == "blocked"
    assert [reason.split(":")[0] for reason in result.reasons] == ["state_unavailable"]
    assert _buys(fake_broker) == []
    assert service.sell("XYZ", 10).status == "done"


def test_daily_halt_blocks_buys_but_not_sells(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_position("XYZ", 10, Decimal(100), current_price=Decimal(100))
    # Equity 95,900 against 100,000 at the previous close: down 4.1%.
    fake_broker.set_account(cash=Decimal(94_900), last_equity=Decimal(100_000))

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "daily_halt")
    assert _buys(fake_broker) == []
    assert service.sell("XYZ", 10).status == "done"


def test_kill_switch_blocks_buys(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    state_store.update_halts(
        lambda _: HaltState(Decimal(100_000), kill_switch=True, daily_halt_date=None)
    )

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "kill_switch")
    assert _buys(fake_broker) == []


@pytest.mark.parametrize("names", [[], ["openai"], ["openai", "openai"]])
def test_missing_or_duplicate_reviewers_cannot_clear_a_buy(
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    fake_reviewer: Callable[..., Any],
    names: list[str],
) -> None:
    service = _service(
        fake_broker, state_store, now, *(fake_reviewer(name, APPROVE) for name in names)
    )

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "review")
    assert _buys(fake_broker) == []
    assert state_store.journal_entries() == []


def test_reviewer_error_blocks_the_buy_and_names_the_reviewer(
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    fake_reviewer: Callable[..., Any],
) -> None:
    service = _service(
        fake_broker,
        state_store,
        now,
        fake_reviewer("openai", APPROVE),
        fake_reviewer("xai", TimeoutError("read timed out")),
    )

    result = service.propose_buy(_proposal())

    assert result.status == "blocked"
    assert [reason.split(":")[0] for reason in result.reasons] == ["xai"]
    assert [(r.reviewer, r.verdict) for r in result.reviews] == [
        ("openai", "approve"),
        ("xai", "error"),
    ]
    assert _buys(fake_broker) == []
    [block] = state_store.recent_blocks()
    assert block.reasons == result.reasons
    assert state_store.journal_entries() == []


def test_reviewers_raised_stop_and_size_multiplier_are_applied(
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    fake_reviewer: Callable[..., Any],
) -> None:
    modify = ReviewVerdict.model_validate(
        {
            "verdict": "modify",
            "confidence": "medium",
            "reasons": ["extended from the base"],
            "modifications": {"stop_price": 97, "size_multiplier": 0.5},
        }
    )
    service = _service(
        fake_broker,
        state_store,
        now,
        fake_reviewer("openai", APPROVE),
        fake_reviewer("xai", modify),
    )

    result = service.propose_buy(_proposal())

    # Stop 95 -> 97 still allows the cap's 200 shares; the 0.5 multiplier halves them.
    assert result.status == "placed"
    assert result.order is not None
    assert (result.order.qty, result.order.stop_price) == (100, Decimal(97))
    assert all(reason.startswith("xai: ") for reason in result.reasons)
    [entry] = _buys(fake_broker)
    [stop] = [o for o in fake_broker.get_orders("all") if o.parent_id == entry.id and o.stop_price]
    assert (entry.qty, stop.stop_price) == (100, Decimal(97))
    [journal_entry] = state_store.journal_entries()
    assert (journal_entry.qty, journal_entry.stop_price) == (100, Decimal(97))


@pytest.mark.parametrize("hesitant", [False, True], ids=["approved", "two-hesitant-rejects"])
def test_final_order_is_checked_again_after_the_review(
    fake_broker: FakeBroker,
    state_store: StateStore,
    now: datetime,
    fake_reviewer: Callable[..., Any],
    hesitant: bool,
) -> None:
    vote = (
        ReviewVerdict(verdict="reject", confidence="medium", reasons=["ordinary uncertainty"])
        if hesitant
        else APPROVE
    )

    class SlowReviewer:
        """Returns a nonblocking verdict once the session is 20 minutes from its close."""

        name = "openai"

        def review(self, packet: ReviewPacket) -> ReviewOutcome:
            _near_close(fake_broker, now)
            return ReviewOutcome(reviewer=self.name, verdict=vote)

    service = _service(fake_broker, state_store, now, SlowReviewer(), fake_reviewer("xai", vote))

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "too_late")
    assert _buys(fake_broker) == []
    assert len(result.reviews) == 2


@pytest.mark.parametrize("method", ["get_account", "get_quote", "get_daily_bars", "get_news"])
def test_broker_error_before_submit_blocks_with_broker_error(
    method: str,
    service: TraderService,
    fake_broker: FakeBroker,
    state_store: StateStore,
) -> None:
    fake_broker.fail_next(method)

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "broker_error")
    assert _buys(fake_broker) == []


def test_refused_submit_blocks_with_broker_error(
    service: TraderService, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.fail_next("submit_bracket_buy", BrokerError("insufficient buying power"))

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "broker_error")
    assert _buys(fake_broker) == []
    assert state_store.journal_entries() == []


def test_symbol_without_enough_history_is_blocked_before_review(
    service: TraderService,
    fake_broker: FakeBroker,
    state_store: StateStore,
    reviewers: list[Any],
) -> None:
    fake_broker.set_daily_bars("XYZ", list[Bar]())

    result = service.propose_buy(_proposal())

    _assert_blocked_and_recorded(result, state_store, "min_adv")
    assert [reviewer.packets for reviewer in reviewers] == [[], []]
