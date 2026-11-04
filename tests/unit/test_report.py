import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.models import Bar, EquityPoint, ReviewOutcome, ReviewVerdict
from trader_broker.report import GoLiveReport, build_report
from trader_broker.state import StateStore, init_state

_NEW_YORK = ZoneInfo("America/New_York")

NOW = datetime(2026, 11, 3, 21, 30, tzinfo=UTC)
"""Tuesday 2026-11-03, 16:30 New York time: after the close."""
EIGHT_WEEKS_AGO = date(2026, 9, 8)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker(NOW)


def _state(tmp_path: Path, start: date = EIGHT_WEEKS_AGO) -> StateStore:
    return init_state(tmp_path / "paper" / "state.db", Decimal(100_000), today=start)


def _history(broker: FakeBroker, start: date, equities: list[int]) -> None:
    """Give the account one daily equity point per value in `equities`, from `start`."""
    broker.set_portfolio_history(
        [
            EquityPoint(
                timestamp=datetime(start.year, start.month, start.day, 20, tzinfo=UTC)
                + timedelta(days=offset),
                equity=Decimal(equity),
            )
            for offset, equity in enumerate(equities)
        ]
    )


def _spy(broker: FakeBroker, start: date, start_close: int, end_close: int) -> None:
    """Give SPY a close of `start_close` on `start` and `end_close` on the day before NOW."""
    broker.set_daily_bars(
        "SPY",
        [
            _bar(start - timedelta(days=1), start_close - 10),
            _bar(start, start_close),
            _bar(NOW.date() - timedelta(days=1), end_close),
        ],
    )


def _bar(day: date, close: int) -> Bar:
    return Bar(
        timestamp=datetime(day.year, day.month, day.day, tzinfo=_NEW_YORK),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1_000_000,
    )


def _passing_period(
    tmp_path: Path, broker: FakeBroker, start: date = EIGHT_WEEKS_AGO
) -> StateStore:
    """A paper period that passes every criterion: +10% with a 10% drawdown vs SPY +4%."""
    _history(broker, start, [100_000, 90_000, 110_000])
    _spy(broker, start, 500, 520)
    return _state(tmp_path, start)


def _results(report: GoLiveReport) -> dict[str, bool]:
    return {criterion.name: criterion.passed for criterion in report.criteria}


def test_a_period_meeting_every_criterion_passes(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        report = build_report(state, broker, NOW)

    assert _results(report) == {"time": True, "return": True, "drawdown": True, "integrity": True}
    assert report.passed
    assert (report.paper_start_date, report.as_of) == (EIGHT_WEEKS_AGO, date(2026, 11, 3))
    assert (report.days_elapsed, report.weeks_elapsed) == (56, 8.0)
    assert report.portfolio_return_pct == pytest.approx(0.10)
    assert report.spy_return_pct == pytest.approx(0.04)
    assert report.max_drawdown_pct == pytest.approx(0.10)
    assert report.integrity_violations == 0
    assert "unlocks nothing by itself" in report.note
    assert "unlocks nothing by itself" in report.as_text()


def test_fewer_than_eight_weeks_fails_only_time(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker, start=EIGHT_WEEKS_AGO + timedelta(days=1)) as state:
        report = build_report(state, broker, NOW)

    assert _results(report) == {"time": False, "return": True, "drawdown": True, "integrity": True}
    assert not report.passed


def test_trailing_spy_fails_only_return(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        _spy(broker, EIGHT_WEEKS_AGO, 500, 600)
        report = build_report(state, broker, NOW)

    assert _results(report) == {"time": True, "return": False, "drawdown": True, "integrity": True}
    assert report.spy_return_pct == pytest.approx(0.20)


def test_a_deep_drawdown_fails_only_drawdown(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        _history(broker, EIGHT_WEEKS_AGO, [100_000, 120_000, 84_000, 130_000])
        report = build_report(state, broker, NOW)

    assert _results(report) == {"time": True, "return": True, "drawdown": False, "integrity": True}
    assert report.max_drawdown_pct == pytest.approx(0.30)


def test_an_integrity_violation_fails_only_integrity(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        state.record_integrity_violation(NOW, "foreign_order", "o-1", "not placed by us")
        report = build_report(state, broker, NOW)

    assert _results(report) == {"time": True, "return": True, "drawdown": True, "integrity": False}
    assert report.integrity_violations == 1


def test_exactly_eight_weeks_passes_time(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker, start=NOW.date() - timedelta(weeks=8)) as state:
        report = build_report(state, broker, NOW)

    assert report.days_elapsed == 56
    assert _results(report)["time"]


def test_a_drawdown_of_exactly_25_percent_fails(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        _history(broker, EIGHT_WEEKS_AGO, [100_000, 75_000, 110_000])
        report = build_report(state, broker, NOW)

    assert report.max_drawdown_pct == 0.25
    assert not _results(report)["drawdown"]


def test_a_return_equal_to_spy_fails(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        _spy(broker, EIGHT_WEEKS_AGO, 500, 550)
        report = build_report(state, broker, NOW)

    assert report.portfolio_return_pct == report.spy_return_pct == pytest.approx(0.10)
    assert not _results(report)["return"]


def test_without_portfolio_history_return_and_drawdown_fail(
    tmp_path: Path, broker: FakeBroker
) -> None:
    with _passing_period(tmp_path, broker) as state:
        # Alpaca reports zero equity for days before the account was funded.
        _history(broker, EIGHT_WEEKS_AGO, [0, 0])
        report = build_report(state, broker, NOW)

    assert (report.portfolio_return_pct, report.max_drawdown_pct) == (None, None)
    assert _results(report) == {"time": True, "return": False, "drawdown": False, "integrity": True}
    assert "portfolio n/a vs SPY +4.00%" in report.as_text()


def test_blocks_are_counted_once_per_stored_rule(tmp_path: Path, broker: FakeBroker) -> None:
    with _passing_period(tmp_path, broker) as state:
        state.add_block(
            NOW, "propose_buy", "AAA", ["kill_switch", "cash"], ["kill_switch: x", "cash: y"]
        )
        state.add_block(NOW, "propose_buy", "BBB", ["cash", "cash"], ["cash: y", "cash: z"])
        state.add_block(NOW, "propose_buy", "CCC", ["broker_error"], ["broker_error: timed out"])
        # A reviewer whose name is a rule id still counts as review: the text is not parsed.
        state.add_block(
            NOW,
            "propose_buy",
            "DDD",
            ["review"],
            ["cash: rejected with high confidence: extended", "grok: review failed: timeout"],
        )
        state.add_block(
            NOW, "propose_buy", "EEE", ["review"], ["no reviewer approved (gpt, grok rejected)"]
        )
        report = build_report(state, broker, NOW)

    assert report.blocks_by_reason == {
        "cash": 2,
        "review": 2,
        "kill_switch": 1,
        "broker_error": 1,
    }


def test_reviewer_hit_rate_is_scored_on_settled_trades(tmp_path: Path, broker: FakeBroker) -> None:
    approve, modify, reject = (
        ReviewVerdict(verdict=verdict, confidence="low", reasons=["r"])
        for verdict in ("approve", "modify", "reject")
    )
    with _passing_period(tmp_path, broker) as state:
        # Beat SPY: gpt approved (hit), grok rejected (miss).
        winner = state.add_review(
            NOW,
            "h1",
            [_outcome("gpt", approve), _outcome("grok", reject)],
            "proceed",
            None,
        )
        _settled_trade(state, winner, alpha_pct=0.05)
        # Trailed SPY: gpt approved (miss), grok modified (miss).
        loser = state.add_review(
            NOW, "h2", [_outcome("gpt", approve), _outcome("grok", modify)], "proceed", None
        )
        _settled_trade(state, loser, alpha_pct=-0.02)
        # Blocked: no trade to score.
        state.add_review(
            NOW, "h3", [_outcome("gpt", reject), _outcome("grok", reject)], "block", None
        )
        # Still open: not scored yet.
        still_open = state.add_review(
            NOW,
            "h4",
            [ReviewOutcome(reviewer="gpt", error="timeout"), _outcome("grok", approve)],
            "proceed",
            None,
        )
        _open_trade(state, still_open)
        report = build_report(state, broker, NOW)

    gpt, grok = report.reviewers
    assert (gpt.reviewer, grok.reviewer) == ("gpt", "grok")
    assert gpt.verdicts == {"approve": 2, "modify": 0, "reject": 1, "error": 1}
    assert grok.verdicts == {"approve": 1, "modify": 1, "reject": 2, "error": 0}
    assert (gpt.settled_trades, gpt.hits, gpt.hit_rate) == (2, 1, 0.5)
    assert (grok.settled_trades, grok.hits, grok.hit_rate) == (2, 0, 0.0)


def test_a_reviewer_without_settled_trades_has_no_hit_rate(
    tmp_path: Path, broker: FakeBroker
) -> None:
    with _passing_period(tmp_path, broker) as state:
        approve = ReviewVerdict(verdict="approve", confidence="high", reasons=["r"])
        _open_trade(state, state.add_review(NOW, "h", [_outcome("gpt", approve)], "proceed", None))
        report = build_report(state, broker, NOW)

    assert [(r.settled_trades, r.hit_rate) for r in report.reviewers] == [(0, None)]
    assert "gpt: approve 1, modify 0, reject 0, error 0; no settled trades yet" in report.as_text()


def test_json_output_parses_and_holds_the_same_values_as_the_text(
    tmp_path: Path, broker: FakeBroker
) -> None:
    with _passing_period(tmp_path, broker) as state:
        state.add_block(NOW, "propose_buy", "AAA", ["kill_switch"], ["kill_switch: drawdown"])
        state.record_integrity_violation(NOW, "negative_cash", "account", "cash -5")
        report = build_report(state, broker, NOW)

    parsed = json.loads(report.model_dump_json())
    text = report.as_text()

    assert GoLiveReport.model_validate(parsed) == report
    assert parsed["passed"] is False
    assert parsed["portfolio_return_pct"] == pytest.approx(0.10)
    assert parsed["blocks_by_reason"] == {"kill_switch": 1}
    assert [(c["name"], c["passed"]) for c in parsed["criteria"]] == [
        ("time", True),
        ("return", True),
        ("drawdown", True),
        ("integrity", False),
    ]
    for criterion in parsed["criteria"]:
        status = "PASS" if criterion["passed"] else "FAIL"
        assert f"{status}  {criterion['name']:<9}  {criterion['detail']}" in text
    assert "portfolio +10.00% vs SPY +4.00%" in text
    assert "max drawdown 10.00%" in text
    assert "kill_switch: 1" in text
    assert "Overall: FAIL (1 of 4 failed)" in text


def _outcome(reviewer: str, verdict: ReviewVerdict) -> ReviewOutcome:
    return ReviewOutcome(reviewer=reviewer, verdict=verdict)


def _open_trade(state: StateStore, review_id: int) -> int:
    """Journal an open trade cleared by review `review_id` and return its entry id."""
    return state.add_journal_entry(
        symbol="XYZ",
        client_order_id=f"tb-{review_id}",
        opened_at=NOW - timedelta(days=10),
        qty=10,
        entry_price=Decimal(100),
        stop_price=Decimal(90),
        target_price=Decimal(120),
        thesis="t",
        invalidation="i",
        horizon_days=10,
        review_id=review_id,
    ).id


def _settled_trade(state: StateStore, review_id: int, alpha_pct: float) -> None:
    """Journal a trade cleared by review `review_id` that closed `alpha_pct` ahead of SPY."""
    entry_id = _open_trade(state, review_id)
    state.close_journal_entry(entry_id, NOW - timedelta(days=2), Decimal(101), 0.01)
    state.settle_journal_entry(entry_id, spy_return_pct=0.01 - alpha_pct, alpha_pct=alpha_pct)
