"""The MCP server: sixteen tools over one `TraderService`, and the server's JSON logging.

Read tools are annotated read-only. Every other tool can write: `get_journal` settles
the journal, and the tools that run housekeeping can place stops; those that can sell or
cancel (`get_risk_status` included, through housekeeping) are also marked destructive.
No tool changes caps, mode, the kill switch or credentials: those live in the
environment and the CLI.

Every tool returns a JSON object. A failure the agent can act on (the broker is
unreachable, the state database is missing, an argument is invalid) comes back as
`{"status": "error", "reason": ..., "message": ...}` rather than a traceback; anything
unexpected is logged with its traceback and returned as `internal_error`.

Tool calls run one at a time: FastMCP runs each in a worker thread, and the state
database and the account's housekeeping must not interleave. Every tool that can
write also holds the account lock, which other trader-broker processes on the same
account share; when it cannot be had in time the result is a `busy` error.
"""

import functools
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, TextIO

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from trader_broker.account_lock import AccountBusyError
from trader_broker.brokers.base import BrokerError
from trader_broker.config import Settings
from trader_broker.models import Price, Proposal
from trader_broker.service import TraderService
from trader_broker.state import JournalStatus, StateUnavailableError

JsonObject = dict[str, Any]
"""A tool result: a JSON-serializable object."""

LOG_MAX_BYTES = 10_000_000
"""Size at which the log file rotates."""
LOG_BACKUP_COUNT = 5
"""Rotated log files kept beside the current one."""

REDACTED = "[REDACTED]"
"""What a configured secret is replaced with in every log line."""

_PriceArgument = Annotated[Decimal, Field(allow_inf_nan=True)]
"""A price argument as the tool receives it. NaN and infinities get through to the
tool, so that it reports them as `invalid_request` like every other out-of-range price
(the tool checks each against `Price`)."""

_PRICE = TypeAdapter(Price)

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

_INSTRUCTIONS = (
    "trader-broker is the only path to the brokerage account. The server sizes every buy, "
    "enforces the risk caps and halts, and has each buy reviewed before placing it as a "
    "bracket order; propose_buy never takes a share count. Sells and stop raises are never "
    "blocked by halts."
)

logger = logging.getLogger(__name__)


def build_server(
    service: TraderService, account_lock: AbstractContextManager[object] | None = None
) -> FastMCP:
    """A FastMCP server whose sixteen tools call `service`; run it with `run(transport="stdio")`.

    Every call is logged with its arguments, outcome and duration. Every tool not
    annotated read-only runs inside `account_lock` (an `AccountLock` shared with the
    other processes on the account; None: no cross-process lock).
    """
    server = FastMCP("trader-broker", instructions=_INSTRUCTIONS)
    one_call_at_a_time = threading.Lock()
    writes_lock = account_lock or nullcontext()

    def tool(annotations: ToolAnnotations) -> Callable[[Callable[..., JsonObject]], None]:
        """Register the decorated function as a tool named after it, with `annotations`."""

        def register(function: Callable[..., JsonObject]) -> None:
            @functools.wraps(function)
            def call(**arguments: Any) -> JsonObject:
                with one_call_at_a_time:
                    return _run_logged(function.__name__, arguments, locked)

            def locked(**arguments: Any) -> JsonObject:
                if annotations.readOnlyHint:
                    return function(**arguments)
                with writes_lock:
                    return function(**arguments)

            server.tool(call, annotations=annotations)

        return register

    # Read tools

    @tool(_READ_ONLY)
    def get_account() -> JsonObject:
        """The account's equity, equity at the previous close, cash and buying power."""
        return service.refresh_equity().account.model_dump(mode="json")

    @tool(_READ_ONLY)
    def get_positions() -> JsonObject:
        """Every open position with its quantity, average entry, price and market value."""
        return {"positions": _json(service.read_broker(lambda broker: broker.get_positions()))}

    @tool(_READ_ONLY)
    def get_orders() -> JsonObject:
        """Open orders, newest first; bracket legs are listed with `parent_id` set."""
        return {"orders": _json(service.read_broker(lambda broker: broker.get_orders("open")))}

    @tool(_READ_ONLY)
    def get_quote(symbol: str) -> JsonObject:
        """The latest bid and ask for `symbol`."""
        quote = service.read_broker(lambda broker: broker.get_quote(_symbol(symbol)))
        return quote.model_dump(mode="json")

    @tool(_READ_ONLY)
    def get_bars(
        symbol: str,
        timeframe: Annotated[str, Field(description="1Min, 5Min, 15Min, 1Hour or 1Day")] = "1Day",
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> JsonObject:
        """The latest `limit` bars of `timeframe` for `symbol`, oldest first."""
        bars = service.read_broker(
            lambda broker: broker.get_bars(_symbol(symbol), timeframe, limit)
        )
        return {"bars": _json(bars)}

    @tool(_READ_ONLY)
    def get_news(
        symbols: list[str] | None = None, limit: Annotated[int, Field(ge=1, le=50)] = 10
    ) -> JsonObject:
        """The newest headlines about any of `symbols`, or about the whole market when omitted."""
        wanted = [_symbol(symbol) for symbol in symbols] if symbols else None
        return {"news": _json(service.read_broker(lambda broker: broker.get_news(wanted, limit)))}

    @tool(_READ_ONLY)
    def get_movers(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> JsonObject:
        """Today's top `limit` gainers and top `limit` losers by change since the previous
        close. `change` is in USD per share; `change_pct` is a share of the previous
        close (0.05 means up 5%)."""
        return service.read_broker(lambda broker: broker.get_movers(limit)).model_dump(mode="json")

    @tool(_READ_ONLY)
    def get_clock() -> JsonObject:
        """Whether the market is open now, and the next open and close."""
        return service.read_broker(lambda broker: broker.get_clock()).model_dump(mode="json")

    @tool(_DESTRUCTIVE)
    def get_risk_status() -> JsonObject:
        """Run housekeeping, then report equity, P&L, peak and drawdown, halts, exposure,
        protection repairs, open entry orders and recent blocked buys.

        Housekeeping can change orders: it cancels entries past their time limit and
        sell orders that hold shares back without a stop, places a GTC stop for shares
        no stop covers, and market-sells them when the bid is already at or below that
        stop. `protection_repairs` lists what it did. Every `_pct` field is a share of
        a whole: 0.05 means 5%.
        """
        return service.get_risk_status().model_dump(mode="json")

    @tool(_WRITE)
    def get_journal(
        status: JournalStatus | None = None, limit: Annotated[int, Field(ge=1, le=200)] = 50
    ) -> JsonObject:
        """Journal entries, newest first, after closing and settling the entries of
        symbols no longer held. `pending_lessons` lists settled trades whose lesson has
        not been recorded with `journal_note` yet."""
        return {"entries": _json(service.get_journal(status, limit))}

    # Write tools

    @tool(_WRITE)
    def propose_buy(
        symbol: str,
        stop_price: _PriceArgument,
        target_price: _PriceArgument,
        thesis: str,
        invalidation: str,
        horizon_days: int,
        limit_price: _PriceArgument | None = None,
    ) -> JsonObject:
        """Propose one swing buy; the server sizes, checks, reviews and places it.

        Give the stop and target prices, the thesis (with the next earnings date), what
        would prove it wrong, and the holding horizon in days (1-60). Omit `limit_price`
        for a marketable limit just above the ask. The share count is always computed
        by the server. Returns `placed` with the bracket order, `blocked` with every
        reason, or `unknown` when the broker's reply was lost: do not propose the same
        buy again; housekeeping resolves it and blocks new buys of the symbol until then.
        """
        try:
            proposal = Proposal(
                symbol=_symbol(symbol),
                stop_price=stop_price,
                target_price=target_price,
                thesis=thesis,
                invalidation=invalidation,
                horizon_days=horizon_days,
                limit_price=limit_price,
            )
        except ValidationError as exc:
            return _error("invalid_request", _describe(exc))
        return service.propose_buy(proposal).model_dump(mode="json")

    @tool(_DESTRUCTIVE)
    def sell(symbol: str, qty: int) -> JsonObject:
        """Market-sell `qty` shares of `symbol`; any shares left keep a stop. Never shorts."""
        return service.sell(_symbol(symbol), qty).model_dump(mode="json")

    @tool(_DESTRUCTIVE)
    def close_position(symbol: str) -> JsonObject:
        """Market-sell every share of `symbol` held."""
        return service.close_position(_symbol(symbol)).model_dump(mode="json")

    @tool(_WRITE)
    def tighten_stop(symbol: str, new_stop: _PriceArgument) -> JsonObject:
        """Raise every stop protecting `symbol` shares held to `new_stop`; a stop is
        never lowered."""
        try:
            new_stop = _PRICE.validate_python(new_stop)
        except ValidationError as exc:
            return _error("invalid_request", f"new_stop: {_describe(exc)}")
        return service.tighten_stop(_symbol(symbol), new_stop).model_dump(mode="json")

    @tool(_DESTRUCTIVE)
    def cancel_entry_order(order_id: str) -> JsonObject:
        """Cancel an unfilled buy entry this server placed, with its stop and target legs."""
        return service.cancel_entry_order(order_id).model_dump(mode="json")

    @tool(_WRITE)
    def journal_note(entry_id: int, note: str) -> JsonObject:
        """Record `note` as the lesson of journal entry `entry_id` and clear its pending flag."""
        try:
            entry = service.journal_note(entry_id, note)
        except KeyError:
            return _error("unknown_entry", f"no journal entry has id {entry_id}")
        return entry.model_dump(mode="json")

    return server


def configure_logging(settings: Settings, stream: TextIO = sys.stderr) -> None:
    """Send every log record, one JSON object per line, to `stream` and the log file.

    The file is `settings.log_path`, rotated at `LOG_MAX_BYTES`; its directory is
    created. Every configured API key and secret is replaced with `REDACTED`. Replaces
    the root logger's handlers and routes FastMCP's own logger through them, so nothing
    is logged in another format. Never logs to stdout, which stdio MCP uses for the
    protocol.
    """
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = JsonLogFormatter(_secret_values(settings))
    handlers: list[logging.Handler] = [
        logging.StreamHandler(stream),
        RotatingFileHandler(
            settings.log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
    ]
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)

    # FastMCP installs its own Rich handler on import; send its records through ours.
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.handlers.clear()
    fastmcp_logger.propagate = True


class JsonLogFormatter(logging.Formatter):
    """Formats a record as one line of JSON, with every secret in `secrets` redacted.

    The object holds `time` (UTC, ISO 8601), `level`, `logger`, `message`, any
    `extra` fields the call passed, and `exception` when there is one.
    """

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        fields: JsonObject = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields.update(
            (key, value) for key, value in vars(record).items() if key not in _RECORD_ATTRIBUTES
        )
        if record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)
        line = json.dumps(fields, default=str)
        for secret in self._secrets:
            # A secret with quotes or backslashes appears escaped inside JSON strings.
            line = line.replace(secret, REDACTED).replace(json.dumps(secret)[1:-1], REDACTED)
        return line


_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}
"""Attributes every LogRecord has; anything else on a record came from `extra`."""


def _run_logged(
    name: str, arguments: JsonObject, function: Callable[..., JsonObject]
) -> JsonObject:
    """`function(**arguments)`, with expected failures turned into error results, logged."""
    started = time.monotonic()
    try:
        result = function(**arguments)
    except BrokerError as exc:
        result = _error("broker_error", str(exc))
    except StateUnavailableError as exc:
        result = _error(
            "state_unavailable",
            f"{exc}; `trader-broker init-state` creates a state database",
        )
    except AccountBusyError as exc:
        result = _error("busy", str(exc))
    except Exception:  # the tool boundary: report, never crash the server
        logger.exception("tool %s raised", name)
        result = _error("internal_error", "unexpected server error; see the server log")
    outcome = result.get("status", "ok")
    logger.info(
        "tool %s: %s",
        name,
        outcome,
        extra={
            "event": "tool_call",
            "tool": name,
            "arguments": arguments,
            "outcome": outcome,
            "reason": result.get("reason"),
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )
    return result


def _error(reason: str, message: str) -> JsonObject:
    return {"status": "error", "reason": reason, "message": message}


def _json(value: object) -> Any:
    return to_jsonable_python(value)


def _symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _describe(exc: ValidationError) -> str:
    """Each error in `exc` as `field: message` (only the message for a bare value)."""
    return "; ".join(
        ": ".join(filter(None, [".".join(str(part) for part in error["loc"]), error["msg"]]))
        for error in exc.errors(include_url=False)
    )


def _secret_values(settings: Settings) -> list[str]:
    secrets = (
        settings.alpaca_api_key,
        settings.alpaca_secret_key,
        settings.reviewer_1.api_key,
        settings.reviewer_2.api_key,
    )
    return [secret.get_secret_value() for secret in secrets if secret is not None]
