"""decide(): when the reviews block a buy, and how their modifications change it."""

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from trader_broker.config import Settings
from trader_broker.models import Account, ReviewOutcome, ReviewVerdict, SizedOrder
from trader_broker.review import ReviewDecision, Reviewer, ReviewPacket, decide, run_reviewers
from trader_broker.risk import Caps, Portfolio, size_position

CAPS = Caps.from_settings(Settings())
PORTFOLIO = Portfolio(
    Account(
        equity=Decimal(100_000),
        last_equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
    )
)


def _sized(entry: str = "100", stop: str = "90") -> SizedOrder:
    decision = size_position(
        "XYZ", Decimal(entry), Decimal(stop), Decimal(entry) * 2, PORTFOLIO, CAPS
    )
    assert decision.sized is not None
    return decision.sized


def _verdict(
    verdict: str = "approve", confidence: str = "medium", **modifications: Any
) -> ReviewVerdict:
    return ReviewVerdict.model_validate(
        {
            "verdict": verdict,
            "confidence": confidence,
            "reasons": [f"{verdict} because"],
            "modifications": modifications or None,
        }
    )


def _outcome(name: str, verdict: ReviewVerdict) -> ReviewOutcome:
    return ReviewOutcome(reviewer=name, verdict=verdict)


def _decide(*outcomes: ReviewOutcome, sized: SizedOrder | None = None) -> ReviewDecision:
    return decide(outcomes, sized or _sized(), PORTFOLIO, CAPS)


def test_approve_and_approve_proceeds_unchanged() -> None:
    sized = _sized()

    decision = _decide(_outcome("openai", _verdict()), _outcome("xai", _verdict()), sized=sized)

    assert decision.proceed
    assert decision.order == sized
    assert decision.reasons == []


def test_approve_and_low_confidence_reject_proceeds() -> None:
    decision = _decide(_outcome("openai", _verdict()), _outcome("xai", _verdict("reject", "low")))

    assert decision.proceed


def test_high_confidence_reject_blocks_naming_the_reviewer() -> None:
    decision = _decide(_outcome("openai", _verdict()), _outcome("xai", _verdict("reject", "high")))

    assert not decision.proceed
    assert decision.order is None
    assert decision.reasons == ["xai: rejected with high confidence: reject because"]


def test_two_low_confidence_rejects_block() -> None:
    decision = _decide(
        _outcome("openai", _verdict("reject", "low")),
        _outcome("xai", _verdict("reject", "low")),
    )

    assert not decision.proceed
    assert decision.reasons == ["no reviewer approved (openai, xai rejected)"]


def test_an_error_blocks_naming_the_reviewer() -> None:
    decision = _decide(
        _outcome("openai", _verdict()),
        ReviewOutcome(reviewer="xai", error="API error: timed out (after one retry)"),
    )

    assert not decision.proceed
    assert decision.reasons == ["xai: review failed: API error: timed out (after one retry)"]


def test_raised_stop_and_multiplier_shrink_the_order() -> None:
    sized = _sized(entry="100", stop="90")
    assert sized.qty == 200  # bound by the 20% symbol cap

    decision = _decide(
        _outcome("openai", _verdict("modify", stop_price=92)),
        _outcome("xai", _verdict("modify", size_multiplier=0.5)),
        sized=sized,
    )

    assert decision.proceed
    assert decision.order is not None
    assert decision.order.stop_price == Decimal("92.00")
    assert decision.order.qty == 100
    assert decision.order.notional == Decimal("10000.00")
    assert decision.order.risk_usd == Decimal("800.00")
    assert decision.reasons == ["openai: stop raised to 92.00", "xai: size multiplied by 0.5"]
    assert decision.order.bound_by == "review"


def test_raised_stop_never_grows_the_order() -> None:
    sized = _sized(entry="50", stop="40")
    assert sized.qty == 300  # bound by the 3% risk limit; at stop 45 it would allow 400

    decision = _decide(
        _outcome("openai", _verdict("modify", stop_price=45)),
        _outcome("xai", _verdict()),
        sized=sized,
    )

    assert decision.order is not None
    assert decision.order.stop_price == Decimal("45.00")
    assert decision.order.qty == 300
    assert decision.order.bound_by == "risk_size"  # the review did not change the size


def test_lower_stop_is_ignored() -> None:
    decision = _decide(
        _outcome("openai", _verdict("modify", stop_price=85)), _outcome("xai", _verdict())
    )

    assert decision.order is not None
    assert decision.order.stop_price == Decimal("90.00")
    assert decision.reasons == []


@pytest.mark.parametrize("stop", [100, 101])
def test_stop_at_or_above_entry_is_ignored(stop: int) -> None:
    decision = _decide(
        _outcome("openai", _verdict("modify", stop_price=stop)), _outcome("xai", _verdict())
    )

    assert decision.order is not None
    assert decision.order.stop_price == Decimal("90.00")


def test_highest_stop_and_smallest_multiplier_win() -> None:
    decision = _decide(
        _outcome("openai", _verdict("modify", stop_price=93, size_multiplier=0.8)),
        _outcome("xai", _verdict("modify", stop_price=92, size_multiplier=0.5)),
    )

    assert decision.order is not None
    assert decision.order.stop_price == Decimal("93.00")
    assert decision.order.qty == 100
    # Only the changes in the final order are reported, not the ones they superseded.
    assert decision.reasons == ["openai: stop raised to 93.00", "xai: size multiplied by 0.5"]


def test_target_change_is_advisory_only() -> None:
    sized = _sized()

    decision = _decide(
        _outcome("openai", _verdict("modify", target_price=150)),
        _outcome("xai", _verdict()),
        sized=sized,
    )

    assert decision.order == sized


def test_multiplier_leaving_no_share_blocks() -> None:
    sized = SizedOrder.build(
        "XYZ", 1, Decimal(100), Decimal(90), Decimal(200), Decimal(100_000), "cash"
    )

    decision = _decide(
        _outcome("openai", _verdict("modify", size_multiplier=0.5)),
        _outcome("xai", _verdict()),
        sized=sized,
    )

    assert not decision.proceed
    assert decision.reasons == ["openai: size multiplier 0.5 leaves no whole share of 1"]


def test_stop_raised_inside_minimum_distance_blocks() -> None:
    decision = _decide(
        _outcome("openai", _verdict("modify", stop_price="99.80")), _outcome("xai", _verdict())
    )

    assert not decision.proceed
    assert len(decision.reasons) == 1
    assert decision.reasons[0].startswith("openai: raised stop 99.80 fails sizing: stop_invalid")


def test_reviewers_run_concurrently(
    fake_reviewer: Callable[..., Reviewer], review_packet: ReviewPacket
) -> None:
    reviewers = [
        fake_reviewer("openai", _verdict(), delay_s=0.3),
        fake_reviewer("xai", _verdict("reject", "low"), delay_s=0.3),
    ]

    started = time.monotonic()
    outcomes = run_reviewers(reviewers, review_packet)
    elapsed_s = time.monotonic() - started

    assert [outcome.reviewer for outcome in outcomes] == ["openai", "xai"]
    assert elapsed_s < 0.5


def test_reviewer_that_raises_gives_an_error_that_blocks(
    fake_reviewer: Callable[..., Reviewer], review_packet: ReviewPacket
) -> None:
    reviewers = [fake_reviewer("openai", _verdict()), fake_reviewer("xai", RuntimeError("boom"))]

    outcomes = run_reviewers(reviewers, review_packet)
    decision = decide(outcomes, _sized(), PORTFOLIO, CAPS)

    assert outcomes[1].error == "reviewer raised RuntimeError: boom"
    assert decision.reasons == ["xai: review failed: reviewer raised RuntimeError: boom"]


def test_invalid_json_reviewer_blocks(
    fake_reviewer: Callable[..., Reviewer], review_packet: ReviewPacket
) -> None:
    reviewers = [fake_reviewer("openai", _verdict()), fake_reviewer("xai", "invalid_json")]

    outcomes = run_reviewers(reviewers, review_packet)

    assert not decide(outcomes, _sized(), PORTFOLIO, CAPS).proceed
