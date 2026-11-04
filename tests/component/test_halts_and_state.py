"""Halts and the state database, through the MCP server and the operator CLI.

The daily halt and kill switch are latched by ordinary tool calls and must hold across
recoveries and server restarts; only the CLI clears the kill switch. A missing or
corrupted state database blocks buys while every exit and read keeps working.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from trader_broker.brokers.fake import FakeBroker
from trader_broker.cli import main
from trader_broker.state import StateStore

from .conftest import (
    MarketTime,
    ServerFactory,
    at,
    buy_arguments,
    call_tool,
    dec,
    place_filled_buy,
)


@pytest.fixture
def state_dir(state_store: StateStore, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points the CLI at `state_store`'s file (`<state_dir>/paper/state.db`)."""
    directory = state_store.path.parent.parent
    monkeypatch.setenv("TRADER_BROKER_STATE_DIR", str(directory))
    return directory


def run_cli(broker: FakeBroker, market_time: MarketTime, *argv: str) -> int:
    return main(list(argv), connect=lambda settings: broker, now=market_time)


def first_rule(result: dict[str, object]) -> str:
    reasons = result["reasons"]
    assert isinstance(reasons, list)
    return str(reasons[0]).split(":", 1)[0]


def test_daily_halt_holds_through_the_day_and_clears_the_next(
    make_server: ServerFactory, fake_broker: FakeBroker, market_time: MarketTime
) -> None:
    server = make_server()
    place_filled_buy(server, fake_broker, stop_price=80)  # 150 shares; $85k cash left
    fake_broker.set_account(cash=Decimal(80_900), last_equity=Decimal(100_000))  # -4.1%

    assert call_tool(server, "get_risk_status")["daily_halt"] is True
    assert first_rule(call_tool(server, "propose_buy", **buy_arguments())) == "daily_halt"

    fake_broker.set_account(cash=Decimal(82_000), last_equity=Decimal(100_000))  # -3%
    still_halted = call_tool(server, "propose_buy", **buy_arguments())
    sold = call_tool(server, "sell", symbol="XYZ", qty=50)

    assert first_rule(still_halted) == "daily_halt"
    assert sold["status"] == "done"

    market_time.set(at(15, 30, day=4))
    fake_broker.set_account(cash=fake_broker.get_account().cash)  # the new prior close
    next_day = call_tool(server, "propose_buy", **buy_arguments())

    assert next_day["status"] == "placed", next_day
    assert call_tool(server, "get_risk_status")["daily_halt"] is False


def test_kill_switch_survives_recovery_and_restart_until_the_cli_resets_it(
    make_server: ServerFactory,
    fake_broker: FakeBroker,
    state_store: StateStore,
    market_time: MarketTime,
    state_dir: Path,
) -> None:
    server = make_server()
    fake_broker.set_account(cash=Decimal(120_000))
    assert dec(call_tool(server, "get_account")["equity"]) == 120_000

    fake_broker.set_account(cash=Decimal(95_900))  # 20.08% below the 120k peak
    status = call_tool(server, "get_risk_status")
    assert (status["kill_switch"], dec(status["peak"])) == (True, 120_000)

    fake_broker.set_account(cash=Decimal(119_000))
    assert first_rule(call_tool(server, "propose_buy", **buy_arguments())) == "kill_switch"

    with StateStore.open(state_store.path) as reopened:
        restarted = make_server(state=reopened)
        assert first_rule(call_tool(restarted, "propose_buy", **buy_arguments())) == ("kill_switch")

        assert run_cli(fake_broker, market_time, "reset-kill-switch") == 0

        status = call_tool(restarted, "get_risk_status")
        assert (status["kill_switch"], dec(status["peak"])) == (False, 119_000)
        assert call_tool(restarted, "propose_buy", **buy_arguments())["status"] == "placed"


def test_without_a_state_database_buys_block_and_exits_and_reads_work(
    make_server: ServerFactory, fake_broker: FakeBroker
) -> None:
    fake_broker.set_account(cash=Decimal(80_000))
    fake_broker.set_position("XYZ", 200, Decimal(100))
    server = make_server(state=None)

    status = call_tool(server, "get_risk_status")  # housekeeping places a stop at 92
    blocked = call_tool(server, "propose_buy", **buy_arguments())

    assert status["state_available"] is False
    assert first_rule(blocked) == "state_unavailable"
    assert call_tool(server, "get_quote", symbol="XYZ")["symbol"] == "XYZ"
    assert call_tool(server, "tighten_stop", symbol="XYZ", new_stop=95)["status"] == "done"
    assert call_tool(server, "sell", symbol="XYZ", qty=50)["status"] == "done"
    assert call_tool(server, "get_journal")["reason"] == "state_unavailable"


def test_state_corrupted_while_serving_fails_closed_and_init_state_needs_force(
    make_server: ServerFactory,
    fake_broker: FakeBroker,
    state_store: StateStore,
    market_time: MarketTime,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = make_server()
    place_filled_buy(server, fake_broker, stop_price=80)
    state_store.path.write_bytes(b"this is not a sqlite database" * 200)

    blocked = call_tool(server, "propose_buy", **buy_arguments())

    assert first_rule(blocked) == "state_unavailable"
    assert call_tool(server, "get_positions")["positions"][0]["qty"] == 150
    assert call_tool(server, "tighten_stop", symbol="XYZ", new_stop=90)["status"] == "done"
    assert call_tool(server, "sell", symbol="XYZ", qty=50)["status"] == "done"

    capsys.readouterr()
    assert run_cli(fake_broker, market_time, "status") == 0
    assert "state database unavailable: buys blocked" in capsys.readouterr().out

    assert run_cli(fake_broker, market_time, "init-state") == 1
    assert "--force" in capsys.readouterr().err
    assert state_store.path.read_bytes().startswith(b"this is not")

    assert run_cli(fake_broker, market_time, "init-state", "--force") == 0
    with StateStore.open(state_store.path) as fresh:
        assert fresh.halts().peak == fake_broker.get_account().equity
        assert call_tool(make_server(state=fresh), "get_risk_status")["state_available"]
