"""`trader-broker serve` as Hermes runs it: a separate process speaking MCP over stdio.

The process gets dummy keys and is only asked things that need no broker call, so no
request leaves the machine. It checks what only the real entry point shows: the
installed console script, the live-mode guard at start, paper mode and the per-mode
paths by default, and JSON logs without secrets on stderr and in the log file.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

TRADER_BROKER = Path(sys.executable).with_name("trader-broker")

SECRETS = {
    "ALPACA_API_KEY": "PKDUMMYALPACAKEY01",
    "ALPACA_SECRET_KEY": "dummy-alpaca-secret-02",
    "OPENAI_API_KEY": "sk-dummy-openai-03",
    "XAI_API_KEY": "xai-dummy-04",
}

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
WRITE_TOOLS = {
    "get_risk_status",
    "get_journal",
    "propose_buy",
    "sell",
    "close_position",
    "tighten_stop",
    "cancel_entry_order",
    "journal_note",
}


@pytest.fixture
def server_env(tmp_path: Path) -> dict[str, str]:
    """A clean environment for `serve`: dummy keys, both reviewer models, a temp state dir."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "TRADER_BROKER_STATE_DIR": str(tmp_path / "state"),
        "REVIEWER_1_MODEL": "gpt-test",
        "REVIEWER_2_MODEL": "grok-test",
        **SECRETS,
    }


def _files_under(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


def test_served_process_lists_the_tools_and_logs_json_without_secrets(
    server_env: dict[str, str], tmp_path: Path
) -> None:
    stderr_path = tmp_path / "stderr.log"
    transport = StdioTransport(str(TRADER_BROKER), ["serve"], env=server_env, log_file=stderr_path)
    leaked = " ".join(SECRETS.values())

    async def session() -> tuple[list[Any], Any]:
        async with Client(transport) as client:
            tools = await client.list_tools()
            # Fails validation inside the tool, before any broker call.
            refused = await client.call_tool(
                "propose_buy",
                {
                    "symbol": "XYZ",
                    "stop_price": 95,
                    "target_price": 120,
                    "thesis": leaked,
                    "invalidation": "never",
                    "horizon_days": 90,
                },
            )
            return tools, refused.structured_content

    tools, refused = asyncio.run(session())

    assert {tool.name for tool in tools} == READ_TOOLS | WRITE_TOOLS
    hints = {tool.name: tool.annotations.readOnlyHint for tool in tools if tool.annotations}
    assert hints == {name: name in READ_TOOLS for name in READ_TOOLS | WRITE_TOOLS}
    assert refused["reason"] == "invalid_request"

    state_dir = Path(server_env["TRADER_BROKER_STATE_DIR"])
    log_file = state_dir / "paper" / "logs" / "trader-broker.log"
    lock_file = state_dir / "paper" / "trader-broker.lock"  # taken by the write tool
    # Paper mode, and no state database created.
    assert sorted(_files_under(state_dir)) == sorted([log_file, lock_file])
    logged_lines = log_file.read_text().splitlines()
    records = [json.loads(line) for line in logged_lines]
    stderr_lines = [line for line in stderr_path.read_text().splitlines() if line.strip()]
    assert [json.loads(line) for line in stderr_lines] == records
    assert any("init-state" in record["message"] for record in records)
    [tool_call] = [record for record in records if record.get("event") == "tool_call"]
    assert (tool_call["tool"], tool_call["outcome"]) == ("propose_buy", "error")
    everything_logged = log_file.read_text() + stderr_path.read_text()
    for secret in SECRETS.values():
        assert secret not in everything_logged


@pytest.mark.parametrize("ack", [None, "", "yes", "i_accept_real_money_risk"])
def test_live_mode_without_the_exact_ack_refuses_to_start(
    server_env: dict[str, str], ack: str | None
) -> None:
    env = {**server_env, "TRADER_BROKER_MODE": "live"}
    if ack is not None:
        env["TRADER_BROKER_LIVE_ACK"] = ack

    result = subprocess.run(
        [str(TRADER_BROKER), "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2
    assert "TRADER_BROKER_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK" in result.stderr
    assert result.stdout == ""
    assert not Path(server_env["TRADER_BROKER_STATE_DIR"]).exists()
