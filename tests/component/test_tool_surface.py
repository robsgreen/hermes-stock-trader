"""No MCP tool reaches the caps, the trading mode, the kill switch or the credentials.

Those are set only from the environment and the operator CLI, so the agent must not be
able to name them in any tool call, not even as an extra argument.
"""

import asyncio
from typing import Any

import pytest
from fastmcp import FastMCP

from trader_broker.state import StateStore

from .conftest import REFUSED, buy_arguments, call_tool

TOOL_PARAMETERS = {
    "get_account": set(),
    "get_positions": set(),
    "get_orders": set(),
    "get_quote": {"symbol"},
    "get_bars": {"symbol", "timeframe", "limit"},
    "get_news": {"symbols", "limit"},
    "get_movers": {"limit"},
    "get_clock": set(),
    "get_risk_status": set(),
    "get_journal": {"status", "limit"},
    "propose_buy": {
        "symbol",
        "stop_price",
        "target_price",
        "thesis",
        "invalidation",
        "horizon_days",
        "limit_price",
    },
    "sell": {"symbol", "qty"},
    "close_position": {"symbol"},
    "tighten_stop": {"symbol", "new_stop"},
    "cancel_entry_order": {"order_id"},
    "journal_note": {"entry_id", "note"},
}

WRITE_TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "propose_buy": buy_arguments(),
    "sell": {"symbol": "XYZ", "qty": 1},
    "close_position": {"symbol": "XYZ"},
    "tighten_stop": {"symbol": "XYZ", "new_stop": 96},
    "cancel_entry_order": {"order_id": "order-1"},
    "journal_note": {"entry_id": 1, "note": "lesson"},
}


def test_tools_take_only_trading_inputs(server: FastMCP) -> None:
    tools = asyncio.run(server.list_tools())

    assert {tool.name: set(tool.parameters["properties"]) for tool in tools} == TOOL_PARAMETERS
    assert all(tool.parameters.get("additionalProperties") is False for tool in tools)


@pytest.mark.parametrize("tool", sorted(WRITE_TOOL_ARGUMENTS))
@pytest.mark.parametrize(
    "smuggled",
    [
        {"mode": "live"},
        {"kill_switch": False},
        {"risk_pct": 0.5},
        {"symbol_cap_pct": 1.0},
        {"api_key": "sk-attacker"},
    ],
    ids=lambda smuggled: next(iter(smuggled)),
)
def test_write_tools_refuse_arguments_naming_settings(
    server: FastMCP, state_store: StateStore, tool: str, smuggled: dict[str, Any]
) -> None:
    halts_before = state_store.halts()

    with pytest.raises(REFUSED):
        call_tool(server, tool, **WRITE_TOOL_ARGUMENTS[tool], **smuggled)

    assert state_store.halts() == halts_before
    assert call_tool(server, "get_risk_status")["mode"] == "paper"
