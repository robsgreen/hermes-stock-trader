"""The MCP tool surface: names, annotations, housekeeping and structured errors."""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from trader_broker.account_lock import AccountLock
from trader_broker.brokers import BrokerError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.config import Settings
from trader_broker.server import build_server
from trader_broker.service import HousekeepingResult, TraderService
from trader_broker.state import StateStore

READ_TOOLS = {
    "get_account",
    "get_positions",
    "get_orders",
    "get_quote",
    "get_bars",
    "get_news",
    "get_movers",
    "get_clock",
}
# get_risk_status runs housekeeping, which can cancel and sell; get_journal settles.
DESTRUCTIVE_WRITE_TOOLS = {"sell", "close_position", "cancel_entry_order", "get_risk_status"}
NON_DESTRUCTIVE_WRITE_TOOLS = {"propose_buy", "tighten_stop", "journal_note", "get_journal"}


def call(server: FastMCP, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(tool, arguments or {}))
    assert result.structured_content is not None
    return result.structured_content


@pytest.fixture
def server(trader_service: TraderService) -> FastMCP:
    return build_server(trader_service)


@pytest.fixture
def housekeeping_calls(trader_service: TraderService, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts the service's housekeeping passes; one element per pass."""
    calls: list[int] = []
    real_housekeeping = trader_service.housekeeping

    def counted() -> HousekeepingResult:
        calls.append(1)
        return real_housekeeping()

    monkeypatch.setattr(trader_service, "housekeeping", counted)
    return calls


def test_server_exposes_exactly_the_sixteen_tools(server: FastMCP) -> None:
    names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert names == READ_TOOLS | DESTRUCTIVE_WRITE_TOOLS | NON_DESTRUCTIVE_WRITE_TOOLS
    assert len(names) == 16


def test_tool_annotations_match_what_each_tool_does(server: FastMCP) -> None:
    hints = {
        tool.name: (tool.annotations.readOnlyHint, tool.annotations.destructiveHint)
        for tool in asyncio.run(server.list_tools())
        if tool.annotations is not None
    }

    assert hints == {
        **{name: (True, None) for name in READ_TOOLS},
        **{name: (False, True) for name in DESTRUCTIVE_WRITE_TOOLS},
        **{name: (False, False) for name in NON_DESTRUCTIVE_WRITE_TOOLS},
    }


def test_a_write_tool_runs_housekeeping(server: FastMCP, housekeeping_calls: list[int]) -> None:
    result = call(server, "close_position", {"symbol": "XYZ"})

    assert result["reason"] == "no_position"
    assert housekeeping_calls == [1]


def test_a_read_tool_runs_no_housekeeping(server: FastMCP, housekeeping_calls: list[int]) -> None:
    result = call(server, "get_quote", {"symbol": "xyz"})

    assert result["symbol"] == "XYZ"
    assert result["ask_price"] == "100.02"
    assert housekeeping_calls == []


def test_a_read_tool_refreshes_equity_and_moves_the_peak(
    server: FastMCP, fake_broker: FakeBroker, state_store: StateStore
) -> None:
    fake_broker.set_account(cash=Decimal(120_000))

    call(server, "get_clock")

    assert state_store.halts().peak == Decimal(120_000)


def test_a_broker_failure_is_a_structured_error(server: FastMCP, fake_broker: FakeBroker) -> None:
    fake_broker.fail_next("get_account", BrokerError("connection reset"))

    result = call(server, "get_positions")

    assert result == {"status": "error", "reason": "broker_error", "message": "connection reset"}


def test_a_write_tool_waits_for_the_account_lock_and_reports_busy(
    trader_service: TraderService, tmp_path: Path, housekeeping_calls: list[int]
) -> None:
    lock_path = tmp_path / "trader-broker.lock"
    server = build_server(trader_service, account_lock=AccountLock(lock_path, timeout_s=0.2))

    with AccountLock(lock_path):  # another process is mid-housekeeping
        busy = call(server, "get_risk_status")
        read = call(server, "get_clock")
    free = call(server, "get_risk_status")

    assert (busy["status"], busy["reason"]) == ("error", "busy")
    assert read["is_open"] is True
    assert "status" not in free
    assert housekeeping_calls == [1]


def test_an_unknown_journal_entry_is_a_structured_error(server: FastMCP) -> None:
    result = call(server, "journal_note", {"entry_id": 999, "note": "cut it sooner"})

    assert result["status"] == "error"
    assert result["reason"] == "unknown_entry"


def test_journal_tools_without_state_return_state_unavailable(fake_broker: FakeBroker) -> None:
    server = build_server(TraderService(fake_broker, None, Settings(), reviewers=[]))

    result = call(server, "get_journal", {"status": "pending_lessons"})

    assert result["status"] == "error"
    assert result["reason"] == "state_unavailable"
    assert "init-state" in result["message"]


def test_an_invalid_proposal_is_a_structured_error(
    server: FastMCP, housekeeping_calls: list[int]
) -> None:
    result = call(
        server,
        "propose_buy",
        {
            "symbol": "XYZ",
            "stop_price": 95,
            "target_price": 120,
            "thesis": "Breakout on volume; earnings 2026-12-01.",
            "invalidation": "Close back inside the base.",
            "horizon_days": 90,
        },
    )

    assert result["reason"] == "invalid_request"
    assert "horizon_days" in result["message"]
    assert housekeeping_calls == []


def test_a_blocked_buy_returns_its_reasons(server: FastMCP, fake_broker: FakeBroker) -> None:
    fake_broker.set_account(cash=Decimal(100_000), last_equity=Decimal(110_000))

    result = call(
        server,
        "propose_buy",
        {
            "symbol": "XYZ",
            "stop_price": 95,
            "target_price": 120,
            "thesis": "Breakout on volume; earnings 2026-12-01.",
            "invalidation": "Close back inside the base.",
            "horizon_days": 20,
        },
    )

    assert result["status"] == "blocked"
    assert any(reason.startswith("daily_halt") for reason in result["reasons"])
