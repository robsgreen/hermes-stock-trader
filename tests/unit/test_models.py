from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from trader_broker.models import Modifications, Proposal, ReviewOutcome, ReviewVerdict, RiskDecision

_PROPOSAL: dict[str, Any] = {
    "symbol": "XYZ",
    "stop_price": "90",
    "target_price": "120",
    "thesis": "Breakout above resistance.",
    "invalidation": "Close below 95.",
    "horizon_days": 10,
}


def test_proposal_parses_prices_as_decimal() -> None:
    proposal = Proposal.model_validate(_PROPOSAL)

    assert proposal.stop_price == Decimal(90)
    assert proposal.limit_price is None


@pytest.mark.parametrize(
    "override",
    [
        {"thesis": ""},
        {"thesis": "x" * 2001},
        {"invalidation": "x" * 501},
        {"horizon_days": 0},
        {"horizon_days": 61},
        {"stop_price": "0"},
    ],
)
def test_proposal_rejects_out_of_range_fields(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Proposal.model_validate(_PROPOSAL | override)


@pytest.mark.parametrize("multiplier", [0, 1.5])
def test_size_multiplier_must_be_in_zero_one(multiplier: float) -> None:
    with pytest.raises(ValidationError):
        Modifications(size_multiplier=multiplier)
    assert Modifications(size_multiplier=1).size_multiplier == 1


@pytest.mark.parametrize("reasons", [[], ["r"] * 6])
def test_verdict_needs_one_to_five_reasons(reasons: list[str]) -> None:
    with pytest.raises(ValidationError):
        ReviewVerdict(verdict="approve", confidence="high", reasons=reasons)


def test_review_outcome_holds_a_verdict_or_an_error() -> None:
    verdict = ReviewVerdict(verdict="approve", confidence="low", reasons=["ok"])

    assert ReviewOutcome(reviewer="r1", error="timeout").verdict is None
    with pytest.raises(ValidationError):
        ReviewOutcome(reviewer="r1")
    with pytest.raises(ValidationError):
        ReviewOutcome(reviewer="r1", verdict=verdict, error="timeout")


def test_risk_decision_allowed_only_without_failures() -> None:
    assert RiskDecision(allowed=False, failures=["leverage"], sized=None).failures == ["leverage"]
    with pytest.raises(ValidationError):
        RiskDecision(allowed=True, failures=["leverage"], sized=None)
    with pytest.raises(ValidationError):
        RiskDecision(allowed=False, failures=[], sized=None)
    with pytest.raises(ValidationError):
        RiskDecision(allowed=False, failures=["no_such_rule"], sized=None)
