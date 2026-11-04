"""The operator CLI: the serve guard, the market gate, state commands and log redaction."""

import asyncio
import functools
import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader_broker import cli
from trader_broker.account_lock import AccountLock
from trader_broker.brokers import BrokerError
from trader_broker.brokers.fake import FakeBroker
from trader_broker.cli import main
from trader_broker.config import Settings
from trader_broker.models import TradingDay
from trader_broker.server import REDACTED, build_server, configure_logging
from trader_broker.service import TraderService
from trader_broker.state import HaltState, StateStore, init_state


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRADER_BROKER_STATE_DIR", str(tmp_path))
    return tmp_path


def session(day: date, close_hour_utc: int = 21) -> TradingDay:
    return TradingDay(
        date=day,
        open=datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC),
        close=datetime(day.year, day.month, day.day, close_hour_utc, tzinfo=UTC),
    )


def run_gate(broker: FakeBroker, now: datetime) -> int:
    return main(["market-open-gate"], connect=lambda settings: broker, now=lambda: now)


def test_serve_in_live_mode_without_the_ack_exits_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADER_BROKER_MODE", "live")

    exit_code = main(["serve"])

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "TRADER_BROKER_LIVE_ACK" in captured.err
    assert captured.out == ""


def test_serve_waits_for_the_account_lock_well_inside_the_mcp_call_timeout(
    state_dir: Path, fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "REVIEWER_1_MODEL": "model-1",
        "OPENAI_API_KEY": "key-1",
        "REVIEWER_2_MODEL": "model-2",
        "XAI_API_KEY": "key-2",
    }.items():
        monkeypatch.setenv(name, value)
    lock_waits: list[float] = []

    def account_lock(path: Path, timeout_s: float = 300.0) -> AccountLock:
        lock_waits.append(timeout_s)
        return AccountLock(path, timeout_s)

    class NotRun:
        def run(self, **_: object) -> None:
            pass

    monkeypatch.setattr(cli, "AccountLock", account_lock)
    monkeypatch.setattr(cli, "build_server", lambda *_, **__: NotRun())
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)

    assert main(["serve"], connect=lambda settings: fake_broker) == 0

    # A call that waits this long and then runs a full buy still answers before
    # Hermes's 300 s call timeout, so the agent sees `busy` rather than a timeout.
    assert lock_waits == [60.0]


@pytest.mark.parametrize(
    ("now", "calendar", "wake"),
    [
        pytest.param(
            datetime(2026, 11, 3, 14, 0, tzinfo=UTC),  # 09:00 New York, a Tuesday
            [session(date(2026, 11, 3))],
            True,
            id="trading-day-at-0900",
        ),
        pytest.param(
            datetime(2026, 11, 26, 14, 0, tzinfo=UTC),  # Thanksgiving
            [session(date(2026, 11, 25)), session(date(2026, 11, 27), close_hour_utc=18)],
            False,
            id="holiday",
        ),
        pytest.param(
            datetime(2026, 11, 7, 14, 0, tzinfo=UTC),  # Saturday
            [session(date(2026, 11, 6)), session(date(2026, 11, 9))],
            False,
            id="weekend",
        ),
        pytest.param(
            datetime(2026, 11, 27, 20, 45, tzinfo=UTC),  # 15:45 New York, closed at 13:00
            [session(date(2026, 11, 27), close_hour_utc=18)],
            True,
            id="early-close-day-at-1545",
        ),
        pytest.param(
            datetime(2026, 11, 4, 2, 0, tzinfo=UTC),  # 21:00 New York on Tuesday the 3rd
            [session(date(2026, 11, 3))],
            True,
            id="new-york-date-not-utc-date",
        ),
    ],
)
def test_market_open_gate_wakes_the_agent_only_on_trading_days(
    now: datetime,
    calendar: list[TradingDay],
    wake: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broker = FakeBroker(now)
    broker.set_calendar(calendar)

    exit_code = run_gate(broker, now)

    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert exit_code == 0
    assert json.loads(last_line) == {"wakeAgent": wake}


def test_market_open_gate_wakes_the_agent_when_the_calendar_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
    broker = FakeBroker(now)
    broker.fail_next("get_calendar", BrokerError("503 service unavailable"))

    exit_code = run_gate(broker, now)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out.strip().splitlines()[-1]) == {"wakeAgent": True}
    assert "503 service unavailable" in captured.err


def test_init_state_refuses_an_existing_database_unless_forced(
    state_dir: Path, fake_broker: FakeBroker, now: datetime, capsys: pytest.CaptureFixture[str]
) -> None:
    def init(*flags: str) -> int:
        return main(["init-state", *flags], connect=lambda settings: fake_broker, now=lambda: now)

    db_path = state_dir / "paper" / "state.db"

    assert init() == 0
    with StateStore.open(db_path) as state:
        state.update_halts(
            lambda _: HaltState(Decimal(90_000), kill_switch=True, daily_halt_date=None)
        )

    assert init() == 1
    assert "already exists" in capsys.readouterr().err
    with StateStore.open(db_path) as state:
        assert state.halts().kill_switch

    assert init("--force") == 0
    with StateStore.open(db_path) as state:
        assert state.halts().peak == Decimal(100_000)
        assert not state.halts().kill_switch


def test_reset_kill_switch_clears_it_and_resets_the_peak_to_current_equity(
    state_dir: Path, fake_broker: FakeBroker, now: datetime
) -> None:
    db_path = state_dir / "paper" / "state.db"
    with init_state(db_path, Decimal(130_000)) as state:
        state.update_halts(
            lambda _: HaltState(Decimal(130_000), kill_switch=True, daily_halt_date=None)
        )

    exit_code = main(["reset-kill-switch"], connect=lambda settings: fake_broker, now=lambda: now)

    assert exit_code == 0
    with StateStore.open(db_path) as state:
        assert not state.halts().kill_switch
        assert state.halts().peak == Decimal(100_000)


def test_reset_kill_switch_without_a_database_fails_with_a_message(
    state_dir: Path, fake_broker: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["reset-kill-switch"], connect=lambda settings: fake_broker)

    assert exit_code == 1
    assert "state.db" in capsys.readouterr().err


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    fastmcp_logger = logging.getLogger("fastmcp")
    saved = (list(root.handlers), root.level, list(fastmcp_logger.handlers))
    saved_propagate = fastmcp_logger.propagate
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    fastmcp_logger.handlers[:] = saved[2]
    fastmcp_logger.propagate = saved_propagate


@pytest.mark.usefixtures("restore_logging")
def test_a_tool_call_log_line_contains_no_configured_secret(
    tmp_path: Path, fake_broker: FakeBroker, state_store: StateStore, now: datetime
) -> None:
    secrets = {
        "ALPACA_API_KEY": "PKALPACAKEY123",
        "ALPACA_SECRET_KEY": "alpaca-secret-456",
        "OPENAI_API_KEY": "sk-openai-789",
        "XAI_API_KEY": 'xai-"quoted"-012',
    }
    settings = Settings.from_env({"TRADER_BROKER_STATE_DIR": str(tmp_path), **secrets})
    stderr = io.StringIO()
    configure_logging(settings, stream=stderr)
    server = build_server(TraderService(fake_broker, state_store, settings, [], now=lambda: now))

    leaked_note = " ".join(secrets.values())
    asyncio.run(server.call_tool("journal_note", {"entry_id": 1, "note": leaked_note}))
    asyncio.run(server.call_tool("get_quote", {"symbol": secrets["ALPACA_API_KEY"]}))

    logged = stderr.getvalue() + settings.log_path.read_text()
    tool_calls = [
        record
        for record in map(json.loads, stderr.getvalue().splitlines())
        if record.get("event") == "tool_call"
    ]
    assert [record["tool"] for record in tool_calls] == ["journal_note", "get_quote"]
    assert REDACTED in logged
    for secret in secrets.values():
        assert secret not in logged
        assert json.dumps(secret)[1:-1] not in logged


def test_status_prints_the_risk_status_for_a_person(
    state_dir: Path, fake_broker: FakeBroker, now: datetime, capsys: pytest.CaptureFixture[str]
) -> None:
    init_state(state_dir / "paper" / "state.db", Decimal(100_000)).close()

    exit_code = main(["status"], connect=lambda settings: fake_broker, now=lambda: now)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Mode: paper" in out
    assert "Equity: $100,000.00" in out
    assert "Kill switch: clear  daily halt: clear" in out
    assert "Recent blocks: none" in out


def test_status_waits_for_a_running_server_and_fails_when_it_stays_busy(
    state_dir: Path,
    fake_broker: FakeBroker,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_state(state_dir / "paper" / "state.db", Decimal(100_000)).close()
    monkeypatch.setattr(cli, "AccountLock", functools.partial(AccountLock, timeout_s=0.1))
    fake_broker.set_position("XYZ", 100, Decimal(100))

    with AccountLock(state_dir / "paper" / "trader-broker.lock"):  # a server mid-housekeeping
        exit_code = main(["status"], connect=lambda settings: fake_broker, now=lambda: now)

    assert exit_code == 1
    assert "trader-broker status: another trader-broker process" in capsys.readouterr().err
    assert fake_broker.get_orders("all") == []  # no repair stop was placed meanwhile


def test_report_prints_json_when_asked(
    state_dir: Path, fake_broker: FakeBroker, now: datetime, capsys: pytest.CaptureFixture[str]
) -> None:
    init_state(state_dir / "paper" / "state.db", Decimal(100_000), today=now.date()).close()

    exit_code = main(["report", "--json"], connect=lambda settings: fake_broker, now=lambda: now)

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["paper_start_date"] == now.date().isoformat()
    assert report["passed"] is False
