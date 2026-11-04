"""The `trader-broker` command line.

`serve` runs the MCP server on stdio. `status`, `init-state`, `reset-kill-switch` and
`report` are the operator's tools: the only way to create the state database or clear
the kill switch. `market-open-gate` tells Hermes' cron whether today is a trading day.

`smoke-test` checks that the Alpaca paper account accepts the orders and serves the
data the server relies on. `run_smoke_test` holds its steps so the opt-in paper test
runs exactly the same checks.

Every subcommand reads its settings from the environment, live-mode guard included,
and exits 2 on a configuration error and 1 when the broker or the state database
fails it.
"""

import argparse
import json
import logging
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trader_broker.account_lock import AccountBusyError, AccountLock
from trader_broker.brokers import Broker, BrokerError
from trader_broker.brokers.alpaca import AlpacaBroker
from trader_broker.config import ConfigError, Settings
from trader_broker.market import market_date
from trader_broker.models import Bar, BracketOrderRequest, Order, round_price
from trader_broker.report import build_report
from trader_broker.review import build_reviewer
from trader_broker.server import build_server, configure_logging
from trader_broker.service import TraderService
from trader_broker.state import StateStore, StateUnavailableError, init_state

DEFAULT_SMOKE_SYMBOL = "SPY"
"""A liquid symbol priced well above $5, so the test order's prices stay valid."""

logger = logging.getLogger(__name__)

_MIN_DAILY_BARS = 20
_ENTRY_DISCOUNT = Decimal("0.5")
"""The test entry's limit as a share of the last close: far enough below that it never fills."""
_STOP_SHARE_OF_ENTRY = Decimal("0.9")
_TARGET_SHARE_OF_ENTRY = Decimal("1.2")
_CANCEL_CONFIRM_ATTEMPTS = 10
_CANCEL_CONFIRM_INTERVAL_S = 0.5


@dataclass(frozen=True)
class SmokeCheck:
    """The outcome of one smoke-test step."""

    name: str
    passed: bool
    detail: str
    """What the step saw, or why it failed."""


class _StepFailedError(Exception):
    """A smoke step saw a result other than the one it checks for."""


def run_smoke_test(broker: Broker, symbol: str = DEFAULT_SMOKE_SYMBOL) -> list[SmokeCheck]:
    """Run every smoke step against `broker` and return one result per step, in order.

    Fetches an IEX quote and SIP-history daily bars for `symbol`, then places a
    1-share bracket buy with a limit at half the last close (so it does not fill),
    checks that the broker kept every price, finds it by client order id, and cancels
    it. A failing step is reported, not raised, and later steps still run where they
    can. The test order is always cancelled on the way out, even if a step raises
    something unexpected; a cancel that fails is reported as a failed check.
    """
    checks: list[SmokeCheck] = []

    def check(name: str, step: Callable[[], str]) -> bool:
        try:
            detail = step()
        except _StepFailedError as exc:
            checks.append(SmokeCheck(name, False, str(exc)))
            return False
        except Exception as exc:  # report every failure as a result; the next steps still run
            checks.append(SmokeCheck(name, False, f"{type(exc).__name__}: {exc}"))
            return False
        checks.append(SmokeCheck(name, True, detail))
        return True

    def quote() -> str:
        latest = broker.get_quote(symbol)
        _require(latest.bid_price > 0 or latest.ask_price > 0, f"empty quote {latest}")
        return (
            f"bid {latest.bid_price}, ask {latest.ask_price} at {latest.timestamp:%Y-%m-%d %H:%M}"
        )

    bars: list[Bar] = []

    def daily_bars() -> str:
        bars.extend(broker.get_daily_bars(symbol, _MIN_DAILY_BARS + 10, sip_history=True))
        _require(len(bars) >= _MIN_DAILY_BARS, f"{len(bars)} bars, need {_MIN_DAILY_BARS}")
        return f"{len(bars)} bars, last close {bars[-1].close} on {bars[-1].timestamp:%Y-%m-%d}"

    check("IEX quote", quote)
    check("SIP-history daily bars", daily_bars)
    if not bars:
        checks.append(SmokeCheck("bracket buy", False, "skipped: no daily close to price it from"))
        return checks

    request = _unfillable_bracket(symbol, bars[-1].close)
    entry: Order | None = None
    cancel_requested = False

    def submit() -> str:
        nonlocal entry
        entry = broker.submit_bracket_buy(request)
        _require(entry.client_order_id == request.client_order_id, f"wrong order {entry}")
        _require(entry.is_open, f"entry status {entry.status}")
        _require(
            entry.limit_price == request.limit_price,
            f"entry limit {entry.limit_price}, sent {request.limit_price}",
        )
        return f"entry {entry.id} {entry.status}, limit {entry.limit_price}"

    def legs() -> str:
        assert entry is not None
        open_orders = broker.get_orders(status="open")
        _require(any(order.id == entry.id for order in open_orders), "entry not among open orders")
        # Alpaca returns an unfilled bracket's legs nested in its entry, held until the
        # entry fills; the adapter flattens them with `parent_id` set. An open read scans
        # orders of every status, so it does not depend on what `status=open` lists.
        entry_legs = [order for order in open_orders if order.parent_id == entry.id]
        stops = [leg.stop_price for leg in entry_legs if leg.type == "stop"]
        targets = [leg.limit_price for leg in entry_legs if leg.type == "limit"]
        seen = f"Alpaca returned {len(entry_legs)} legs (stops {stops}, targets {targets})"
        _require(stops == [request.stop_price], f"{seen}; sent stop {request.stop_price}")
        _require(
            targets == [request.take_profit_price],
            f"{seen}; sent target {request.take_profit_price}",
        )
        return seen

    def find_by_client_id() -> str:
        assert entry is not None
        found = broker.get_order_by_client_id(request.client_order_id)
        _require(found is not None and found.id == entry.id, f"found {found}")
        return f"found {entry.id}"

    def cancel() -> str:
        nonlocal cancel_requested
        assert entry is not None
        broker.cancel_order(entry.id)
        cancel_requested = True
        status = _await_cancel(broker, request.client_order_id)
        _require(status == "canceled", f"status still {status}")
        return f"entry {entry.id} canceled"

    try:
        if check("bracket buy accepted", submit):
            check("open orders list the legs with parent_id", legs)
            check("found by client order id", find_by_client_id)
            check("cancel confirmed", cancel)
    finally:
        if not cancel_requested:
            _cancel_leftover(broker, request.client_order_id, checks)
    return checks


@dataclass(frozen=True)
class _Runtime:
    """How subcommands reach the outside world; tests substitute a fake broker and clock."""

    connect: Callable[[Settings], Broker]
    """Builds the broker client for `settings`; raises ConfigError without credentials."""
    now: Callable[[], datetime]
    """The current timezone-aware time."""


def main(
    argv: Sequence[str] | None = None,
    *,
    connect: Callable[[Settings], Broker] = AlpacaBroker.from_settings,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Run the subcommand named in `argv` (default: the process arguments); return the exit code.

    `connect` builds the broker from the settings and `now` reads the clock.
    """
    parser = argparse.ArgumentParser(prog="trader-broker")
    subcommands = parser.add_subparsers(required=True, metavar="COMMAND", dest="name")

    serve = subcommands.add_parser(
        "serve", help="run the MCP server on stdio (needs Alpaca keys and both reviewers)"
    )
    serve.set_defaults(command=_serve)

    status = subcommands.add_parser(
        "status", help="run housekeeping and print the risk status for a person to read"
    )
    status.set_defaults(command=_status)

    init = subcommands.add_parser(
        "init-state", help="create the state database, with the peak at the current equity"
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="replace an existing database, discarding its journal, reviews and halts",
    )
    init.set_defaults(command=_init_state)

    reset = subcommands.add_parser(
        "reset-kill-switch", help="clear the kill switch and restart the peak at current equity"
    )
    reset.set_defaults(command=_reset_kill_switch)

    report = subcommands.add_parser("report", help="score the paper period against the go-live bar")
    report.add_argument("--json", action="store_true", help="print the report as JSON")
    report.set_defaults(command=_report)

    gate = subcommands.add_parser(
        "market-open-gate",
        help='print {"wakeAgent": true} when today is a trading session, else false',
    )
    gate.set_defaults(command=_market_open_gate)

    smoke = subcommands.add_parser(
        "smoke-test",
        help="place and cancel an unfillable bracket order on the Alpaca paper account",
    )
    smoke.add_argument(
        "--symbol",
        default=DEFAULT_SMOKE_SYMBOL,
        help=f"liquid symbol priced at $5 or more (default {DEFAULT_SMOKE_SYMBOL})",
    )
    smoke.set_defaults(command=_smoke_test)

    args = parser.parse_args(argv)
    command: Callable[[argparse.Namespace, _Runtime], int] = args.command
    try:
        return command(args, _Runtime(connect, now))
    except ConfigError as exc:
        print(f"trader-broker {args.name}: {exc}", file=sys.stderr)
        return 2
    except (BrokerError, StateUnavailableError, FileExistsError, AccountBusyError) as exc:
        print(f"trader-broker {args.name}: {exc}", file=sys.stderr)
        return 1


def _serve(args: argparse.Namespace, runtime: _Runtime) -> int:
    """Run the MCP server until stdin closes.

    Stops before starting on a configuration error: live mode without its
    acknowledgement, missing Alpaca keys, or incomplete reviewer configuration. A
    missing state database does not stop it; buys are blocked until `init-state`
    creates one and the server restarts. Prints nothing to stdout, which carries the
    MCP protocol.
    """
    settings = Settings.from_env()
    settings.require_reviewers()
    broker = runtime.connect(settings)
    reviewers = [
        build_reviewer(reviewer, settings)
        for reviewer in (settings.reviewer_1, settings.reviewer_2)
    ]
    configure_logging(settings)
    state = _open_state_or_none(settings)
    try:
        service = TraderService(broker, state, settings, reviewers, now=runtime.now)
        account_lock = AccountLock(settings.account_lock_path, settings.account_lock_wait_s)
        server = build_server(service, account_lock=account_lock)
        server.run(transport="stdio", show_banner=False)
    finally:
        if state is not None:
            state.close()
    return 0


def _status(args: argparse.Namespace, runtime: _Runtime) -> int:
    """Print `get_risk_status` for a person; it runs housekeeping, which may place orders,
    so it holds the account lock that running servers share."""
    settings = Settings.from_env()
    broker = runtime.connect(settings)
    state = _open_state_or_none(settings)
    try:
        with AccountLock(settings.account_lock_path):
            status = TraderService(broker, state, settings, [], now=runtime.now).get_risk_status()
    finally:
        if state is not None:
            state.close()
    print(status.as_text())
    return 0


def _init_state(args: argparse.Namespace, runtime: _Runtime) -> int:
    settings = Settings.from_env()
    equity = runtime.connect(settings).get_account().equity
    today = market_date(runtime.now())
    with init_state(settings.state_db_path, equity, force=args.force, today=today) as state:
        print(f"created {state.path} with the peak at ${equity:,.2f}")
    return 0


def _reset_kill_switch(args: argparse.Namespace, runtime: _Runtime) -> int:
    settings = Settings.from_env()
    equity = runtime.connect(settings).get_account().equity
    with StateStore.open(settings.state_db_path) as state:
        state.reset_kill_switch(equity)
    print(f"kill switch cleared; peak reset to the current equity ${equity:,.2f}")
    return 0


def _report(args: argparse.Namespace, runtime: _Runtime) -> int:
    settings = Settings.from_env()
    broker = runtime.connect(settings)
    with StateStore.open(settings.state_db_path) as state:
        report = build_report(state, broker, runtime.now())
    print(report.model_dump_json(indent=2) if args.json else report.as_text())
    return 0


def _market_open_gate(args: argparse.Namespace, runtime: _Runtime) -> int:
    """Print whether today, in New York, is a trading session; the JSON is the last line.

    Never checks whether the market is open at this moment, so a job before the open or
    after an early close still runs. When the answer cannot be had (a configuration or
    broker error) the agent is woken anyway: the server still enforces every rule.
    Always exits 0.
    """
    today = market_date(runtime.now())
    try:
        sessions = runtime.connect(Settings.from_env()).get_calendar(today, today)
    except (ConfigError, BrokerError) as exc:
        print(f"trader-broker market-open-gate: {exc}; waking the agent anyway", file=sys.stderr)
        wake = True
    else:
        wake = any(session.date == today for session in sessions)
        print(f"{today} is {'a' if wake else 'not a'} trading session")
    print(json.dumps({"wakeAgent": wake}))
    return 0


def _smoke_test(args: argparse.Namespace, runtime: _Runtime) -> int:
    settings = Settings.from_env()
    if settings.mode != "paper":
        raise ConfigError("smoke-test places real orders; it runs only in paper mode")
    broker = runtime.connect(settings)

    checks = run_smoke_test(broker, args.symbol.upper())
    for result in checks:
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}: {result.detail}")
    return 0 if all(result.passed for result in checks) else 1


def _open_state_or_none(settings: Settings) -> StateStore | None:
    """The mode's state database, or None, with a logged warning, when it is unavailable."""
    try:
        return StateStore.open(settings.state_db_path)
    except StateUnavailableError as exc:
        logger.warning(
            "%s; buys are blocked until `trader-broker init-state` creates it"
            " (a running server picks it up when restarted)",
            exc,
        )
        return None


def _unfillable_bracket(symbol: str, last_close: Decimal) -> BracketOrderRequest:
    """A 1-share bracket buy whose limit sits far enough below `last_close` not to fill."""
    limit_price = round_price(last_close * _ENTRY_DISCOUNT)
    return BracketOrderRequest(
        symbol=symbol,
        qty=1,
        limit_price=limit_price,
        stop_price=round_price(limit_price * _STOP_SHARE_OF_ENTRY),
        take_profit_price=round_price(limit_price * _TARGET_SHARE_OF_ENTRY),
        # Not the server's `tb-` prefix: this order is never in the state store.
        client_order_id=f"smoke-{uuid.uuid4()}",
    )


def _await_cancel(broker: Broker, client_order_id: str) -> str:
    """The order's status once it is `canceled`, or its last status when polling gives up.

    Alpaca cancels asynchronously: the order passes through `pending_cancel` first.
    """
    status = "missing"
    for attempt in range(_CANCEL_CONFIRM_ATTEMPTS):
        if attempt:
            time.sleep(_CANCEL_CONFIRM_INTERVAL_S)
        order = broker.get_order_by_client_id(client_order_id)
        status = order.status if order is not None else "missing"
        if status == "canceled":
            break
    return status


def _cancel_leftover(broker: Broker, client_order_id: str, checks: list[SmokeCheck]) -> None:
    """Cancel the test order if the broker holds it open; add a failed check if that fails.

    Looks the order up by client order id, so it also catches an order whose submit
    timed out after the broker accepted it.
    """
    try:
        order = broker.get_order_by_client_id(client_order_id)
        if order is not None and order.is_open:
            broker.cancel_order(order.id)
    except BrokerError as exc:
        checks.append(
            SmokeCheck(
                "clean up test order",
                False,
                f"{exc}; cancel order {client_order_id} in the Alpaca dashboard",
            )
        )


def _require(condition: bool, failure: str) -> None:
    if not condition:
        raise _StepFailedError(failure)
