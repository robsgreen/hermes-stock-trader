"""`trader-broker report` over a state database the MCP server filled by itself.

A paper week is played through the tools: a buy the review blocks, a buy a rule
blocks, and a buy that is placed, sold at a profit and settled against SPY. The report
must score what the server recorded, the same way in text and in JSON.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.cli import main
from trader_broker.models import EquityPoint
from trader_broker.review import Reviewer
from trader_broker.state import StateStore

from .conftest import (
    APPROVE,
    MarketTime,
    ServerFactory,
    at,
    buy_arguments,
    call_tool,
    daily_close,
    place_filled_buy,
    verdict,
)


@pytest.fixture
def played_week(
    make_server: ServerFactory,
    fake_reviewer: Callable[..., Reviewer],
    fake_broker: FakeBroker,
    state_store: StateStore,
    market_time: MarketTime,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., int]:
    """Plays the week, then returns `report(*flags)`, which runs the CLI report."""
    skeptic = make_server(
        [fake_reviewer("openai", APPROVE), fake_reviewer("xai", verdict("reject", "high"))]
    )
    assert call_tool(skeptic, "propose_buy", **buy_arguments())["status"] == "blocked"
    server = make_server()
    penny = buy_arguments(limit_price="4.99", stop_price="4.80", target_price=6)
    assert call_tool(server, "propose_buy", **penny)["status"] == "blocked"
    spy_history = fake_broker.get_daily_bars("SPY", 260)
    place_filled_buy(server, fake_broker)

    market_time.set(at(15, 30, day=4))
    fake_broker.set_quote("XYZ", bid=Decimal(110), ask=Decimal("110.02"))
    call_tool(server, "close_position", symbol="XYZ")

    market_time.set(at(15, 30, day=5))
    fake_broker.set_daily_bars("SPY", [*spy_history, daily_close(3, 100), daily_close(4, 102)])
    fake_broker.set_portfolio_history(
        [
            EquityPoint(timestamp=datetime(2026, 11, 3, 21, tzinfo=UTC), equity=Decimal(100_000)),
            EquityPoint(timestamp=datetime(2026, 11, 4, 21, tzinfo=UTC), equity=Decimal(102_000)),
            EquityPoint(timestamp=datetime(2026, 11, 5, 21, tzinfo=UTC), equity=Decimal(103_000)),
        ]
    )
    call_tool(server, "get_risk_status")
    monkeypatch.setenv("TRADER_BROKER_STATE_DIR", str(state_store.path.parent.parent))

    def report(*flags: str) -> int:
        return main(["report", *flags], connect=lambda settings: fake_broker, now=market_time)

    return report


def test_report_scores_what_the_server_recorded_in_text_and_json(
    played_week: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert played_week("--json") == 0
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert played_week() == 0
    text = capsys.readouterr().out

    assert {c["name"]: c["passed"] for c in report["criteria"]} == {
        "time": False,
        "return": True,
        "drawdown": True,
        "integrity": True,
    }
    assert report["passed"] is False
    assert report["portfolio_return_pct"] == pytest.approx(0.03)
    assert report["spy_return_pct"] == pytest.approx(0.02)
    assert report["blocks_by_reason"] == {"review": 1, "min_price": 1}
    stats = {r["reviewer"]: r for r in report["reviewers"]}
    assert stats["openai"]["verdicts"] == {"approve": 2, "modify": 0, "reject": 0, "error": 0}
    assert stats["xai"]["verdicts"] == {"approve": 1, "modify": 0, "reject": 1, "error": 0}
    assert {name: (s["settled_trades"], s["hit_rate"]) for name, s in stats.items()} == {
        "openai": (1, 1.0),
        "xai": (1, 1.0),
    }

    for criterion in report["criteria"]:
        verdict_word = "PASS" if criterion["passed"] else "FAIL"
        assert f"{verdict_word}  {criterion['name']:<9}  {criterion['detail']}" in text
    assert "Overall: FAIL (1 of 4 failed)" in text
    assert "  review: 1" in text and "  min_price: 1" in text
    assert "xai: approve 1, modify 0, reject 1, error 0; hit rate 100%" in text


def test_report_without_a_state_database_fails_with_a_message(
    fake_broker: FakeBroker,
    market_time: MarketTime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TRADER_BROKER_STATE_DIR", str(tmp_path / "never-initialised"))

    exit_code = main(["report"], connect=lambda settings: fake_broker, now=market_time)

    assert exit_code == 1
    assert "state.db" in capsys.readouterr().err
    assert not (tmp_path / "never-initialised").exists()
