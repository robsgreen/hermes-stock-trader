"""The SQLite state store: halt latches, order intents, the journal, reviews and blocks.

The store fails closed. Opening a missing, unreadable or unrecognised file raises
`StateUnavailableError` and creates nothing, so a lost file can never silently clear
the kill switch; only `init_state` creates a database. Money amounts are stored as
decimal text and timestamps as ISO 8601 text, so values read back exactly as written.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from trader_broker.market import market_date
from trader_broker.models import (
    BlockRule,
    JournalEntry,
    OrderSide,
    OrderStatus,
    Proposal,
    ReviewOutcome,
    SizedOrder,
)

SCHEMA_VERSION = 2
"""Version of the table layout below; a file with any other version is unavailable."""

OrderRole = Literal["entry", "stop", "sell", "repair"]
"""Why the server placed an order: a bracket entry, the stop that replaced a stop
`tighten_stop` raised, an exit sell, or a stop placed by housekeeping for shares no
stop covered."""

JournalStatus = Literal["open", "closed", "pending_lessons"]
"""A journal filter: entries still open, closed entries, or settled entries whose
lesson has not been recorded yet."""

_SCHEMA = """
CREATE TABLE account_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    equity_peak TEXT NOT NULL,
    kill_switch INTEGER NOT NULL CHECK (kill_switch IN (0, 1)),
    daily_halt_date TEXT,
    paper_start_date TEXT NOT NULL
);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    packet_hash TEXT NOT NULL,
    outcomes TEXT NOT NULL,
    decision TEXT NOT NULL,
    final_order TEXT
);
CREATE TABLE journal (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    target_price TEXT NOT NULL,
    thesis TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    review_id INTEGER REFERENCES reviews (id),
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    exit_price TEXT,
    trade_return_pct REAL,
    spy_return_pct REAL,
    alpha_pct REAL,
    lesson_pending INTEGER NOT NULL DEFAULT 0,
    lesson TEXT
);
CREATE TABLE orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT,
    sizing TEXT,
    proposal TEXT,
    review_id INTEGER REFERENCES reviews (id),
    journal_entry_id INTEGER REFERENCES journal (id),
    created_at TEXT NOT NULL
);
CREATE TABLE blocks (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    tool TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rules TEXT NOT NULL,
    reasons TEXT NOT NULL
);
CREATE TABLE integrity_violations (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail TEXT NOT NULL,
    UNIQUE (kind, subject)
);
"""

_TABLES = frozenset(
    {"account_state", "reviews", "journal", "orders", "blocks", "integrity_violations"}
)


class StateUnavailableError(Exception):
    """The state database is missing, unreadable or not one this version understands.

    New buys must be blocked while state is unavailable; `trader-broker init-state`
    creates a fresh database.
    """


@dataclass(frozen=True)
class HaltState:
    """The stored halt latches, as `risk.check_halts` takes and returns them."""

    peak: Decimal
    """Highest account equity seen, in USD."""
    kill_switch: bool
    daily_halt_date: date | None
    """Trading date of the last daily halt, or None if there has been none."""


@dataclass(frozen=True)
class StoredOrder:
    """An order the server placed, or is about to place, as recorded in state."""

    client_order_id: str
    broker_order_id: str | None
    """None until the broker has acknowledged the order."""
    symbol: str
    side: OrderSide
    role: OrderRole
    status: OrderStatus | None
    """Last broker status seen; None until the broker has acknowledged the order or
    refused it (`rejected`). An entry still None after its submit is unresolved: its
    outcome is unknown until the broker is asked for it by client order id."""
    sizing: SizedOrder | None
    """The sizing at placement, for entries; None for other roles."""
    proposal: Proposal | None
    """The agent's proposal, for entries, so a journal entry can be written once an
    unknown outcome turns out to be a placed order; None for other roles."""
    review_id: int | None
    """The stored review that cleared an entry; None for other roles."""
    journal_entry_id: int | None
    """Journal entry this order protects or exits; an entry order is linked the other
    way, by the journal entry's `client_order_id`."""
    created_at: datetime


@dataclass(frozen=True)
class Block:
    """A tool call the server refused."""

    at: datetime
    tool: str
    symbol: str
    rules: list[BlockRule]
    """The id of each rule that blocked the call, once each, in the order first seen."""
    reasons: list[str]


@dataclass(frozen=True)
class StoredReview:
    """One cross-model review of a proposed buy, as recorded in state."""

    id: int
    created_at: datetime
    packet_hash: str
    """SHA-256 of the review packet the reviewers saw."""
    outcomes: list[ReviewOutcome]
    """Every reviewer's verdict or error."""
    decision: str
    """`proceed` or `block`."""
    final_order: SizedOrder | None
    """The order after the review's changes; None when the review blocked the buy."""


class StateStore:
    """An open connection to one state database.

    Use `StateStore.open` for an existing database or `init_state` to create one, and
    close it when done (it is also a context manager). Any later read or write that
    fails at the SQLite level, such as a file deleted or corrupted while open, raises
    `StateUnavailableError`. A store may be used from any thread, but by one at a time.
    """

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        """Wrap an open connection to `path`; callers use `open` or `init_state`."""
        self._connection = connection
        self.path = path
        """The database file."""

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open the existing database at `path` without ever creating a file.

        Raises StateUnavailableError when the file is missing, unreadable, not a
        SQLite database, or lacks this version's schema.
        """
        try:
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=rw", uri=True, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StateUnavailableError(f"cannot open state database {path}: {exc}") from None
        store = cls(connection, path)
        try:
            store._validate_schema()
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # Halt latches

    def halts(self) -> HaltState:
        """The stored peak, kill switch and daily halt date."""
        row = self._fetch_one("SELECT equity_peak, kill_switch, daily_halt_date FROM account_state")
        assert row is not None  # _validate_schema guarantees the row
        return HaltState(
            peak=Decimal(row["equity_peak"]),
            kill_switch=bool(row["kill_switch"]),
            daily_halt_date=_date_or_none(row["daily_halt_date"]),
        )

    def update_halts(self, update: Callable[[HaltState], HaltState]) -> HaltState:
        """Read the halt latches, store `update(current)`, and return what was stored.

        The read and the write are one transaction that holds the database's write
        lock, so a `reset_kill_switch` from another process lands either before the
        read or after the write, never in between where it would be overwritten. The
        kill switch only latches: a False from `update` leaves a set switch set,
        because only `reset_kill_switch` may clear it. If `update` raises, nothing is
        stored and the exception propagates.
        """
        with self._sqlite_errors(), self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._write_halts(update(self.halts()))
            return self.halts()

    def _write_halts(self, halts: HaltState) -> None:
        """Write the latches inside the caller's transaction; the kill switch only latches."""
        self._connection.execute(
            "UPDATE account_state SET equity_peak = ?, kill_switch = MAX(kill_switch, ?),"
            " daily_halt_date = ?",
            (str(halts.peak), int(halts.kill_switch), _iso_or_none(halts.daily_halt_date)),
        )

    def reset_kill_switch(self, current_equity: Decimal) -> None:
        """Clear the kill switch and restart the peak from `current_equity`."""
        self._execute(
            "UPDATE account_state SET kill_switch = 0, equity_peak = ?", (str(current_equity),)
        )

    def paper_start_date(self) -> date:
        """The date `init_state` created this database; the go-live report counts from it."""
        row = self._fetch_one("SELECT paper_start_date FROM account_state")
        assert row is not None
        return date.fromisoformat(row["paper_start_date"])

    # Orders

    def record_order_intent(
        self,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        role: OrderRole,
        created_at: datetime,
        sizing: SizedOrder | None = None,
        proposal: Proposal | None = None,
        review_id: int | None = None,
        journal_entry_id: int | None = None,
    ) -> None:
        """Record an order before it is submitted, so a lost reply can be looked up.

        An entry passes its `sizing`, `proposal` and `review_id`, which are enough to
        journal it later. Raises ValueError when `client_order_id` is already recorded.
        """
        try:
            self._execute(
                "INSERT INTO orders (client_order_id, symbol, side, role, sizing, proposal,"
                " review_id, journal_entry_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    client_order_id,
                    symbol,
                    side,
                    role,
                    sizing.model_dump_json() if sizing else None,
                    proposal.model_dump_json() if proposal else None,
                    review_id,
                    journal_entry_id,
                    created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"client order id {client_order_id} is already recorded") from None

    def update_order(
        self, client_order_id: str, status: OrderStatus, broker_order_id: str | None = None
    ) -> None:
        """Set a recorded order's broker status and, when given, its broker order id.

        Raises KeyError when `client_order_id` was never recorded.
        """
        updated = self._execute(
            "UPDATE orders SET status = ?, broker_order_id = COALESCE(?, broker_order_id)"
            " WHERE client_order_id = ?",
            (status, broker_order_id, client_order_id),
        )
        if updated == 0:
            raise KeyError(f"client order id {client_order_id} is not recorded")

    def order(self, client_order_id: str) -> StoredOrder | None:
        """The recorded order with `client_order_id`, or None if there is none."""
        row = self._fetch_one("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
        return None if row is None else _stored_order(row)

    def unresolved_entries(self) -> list[StoredOrder]:
        """Entry orders whose submit outcome is unknown (no broker status), oldest first."""
        rows = self._fetch_all(
            "SELECT * FROM orders WHERE role = 'entry' AND status IS NULL ORDER BY created_at"
        )
        return [_stored_order(row) for row in rows]

    # Journal

    def add_journal_entry(
        self,
        *,
        symbol: str,
        client_order_id: str,
        opened_at: datetime,
        qty: int,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        thesis: str,
        invalidation: str,
        horizon_days: int,
        review_id: int | None = None,
    ) -> JournalEntry:
        """Add an open journal entry for a placed buy and return it with its id."""
        entry_id = self._insert(
            "INSERT INTO journal (symbol, client_order_id, opened_at, qty, entry_price,"
            " stop_price, target_price, thesis, invalidation, horizon_days, review_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                client_order_id,
                opened_at.isoformat(),
                qty,
                str(entry_price),
                str(stop_price),
                str(target_price),
                thesis,
                invalidation,
                horizon_days,
                review_id,
            ),
        )
        entry = self.journal_entry(entry_id)
        assert entry is not None
        return entry

    def journal_entry(self, entry_id: int) -> JournalEntry | None:
        """The journal entry with `entry_id`, or None if there is none."""
        row = self._fetch_one("SELECT * FROM journal WHERE id = ?", (entry_id,))
        return None if row is None else JournalEntry.model_validate(dict(row))

    def journal_entry_for_order(self, client_order_id: str) -> JournalEntry | None:
        """The journal entry of the bracket entry `client_order_id`, or None if there is
        none."""
        row = self._fetch_one(
            "SELECT * FROM journal WHERE client_order_id = ? ORDER BY id LIMIT 1",
            (client_order_id,),
        )
        return None if row is None else JournalEntry.model_validate(dict(row))

    def set_journal_stop(self, entry_id: int, stop_price: Decimal) -> None:
        """Set the journal entry's current protective stop.

        Raises KeyError when there is no entry with `entry_id`.
        """
        updated = self._execute(
            "UPDATE journal SET stop_price = ? WHERE id = ?", (str(stop_price), entry_id)
        )
        if updated == 0:
            raise KeyError(f"journal entry {entry_id} does not exist")

    def close_journal_entry(
        self,
        entry_id: int,
        closed_at: datetime,
        exit_price: Decimal | None,
        trade_return_pct: float | None,
        *,
        entry_price: Decimal | None = None,
        qty: int | None = None,
    ) -> None:
        """Mark an open journal entry closed; `settle_journal_entry` may follow.

        `exit_price` and `trade_return_pct` are None for a buy that never filled.
        `entry_price` and `qty`, when given, replace the recorded ones (the order's
        limit and qty) with the entry's fill. Raises KeyError when there is no open
        entry with `entry_id`.
        """
        updated = self._execute(
            "UPDATE journal SET status = 'closed', closed_at = ?, exit_price = ?,"
            " trade_return_pct = ?, entry_price = COALESCE(?, entry_price),"
            " qty = COALESCE(?, qty) WHERE id = ? AND status = 'open'",
            (
                closed_at.isoformat(),
                None if exit_price is None else str(exit_price),
                trade_return_pct,
                None if entry_price is None else str(entry_price),
                qty,
                entry_id,
            ),
        )
        if updated == 0:
            raise KeyError(f"journal entry {entry_id} does not exist or is not open")

    def settle_journal_entry(self, entry_id: int, spy_return_pct: float, alpha_pct: float) -> None:
        """Store a closed entry's SPY comparison and mark its lesson pending.

        Raises KeyError when there is no closed entry with `entry_id`.
        """
        updated = self._execute(
            "UPDATE journal SET spy_return_pct = ?, alpha_pct = ?, lesson_pending = 1"
            " WHERE id = ? AND status = 'closed'",
            (spy_return_pct, alpha_pct, entry_id),
        )
        if updated == 0:
            raise KeyError(f"journal entry {entry_id} does not exist or is not closed")

    def set_journal_lesson(self, entry_id: int, lesson: str) -> None:
        """Store the lesson learned from a journal entry and clear its pending flag.

        Raises KeyError when there is no entry with `entry_id`.
        """
        updated = self._execute(
            "UPDATE journal SET lesson = ?, lesson_pending = 0 WHERE id = ?", (lesson, entry_id)
        )
        if updated == 0:
            raise KeyError(f"journal entry {entry_id} does not exist")

    def journal_entries(
        self, status: JournalStatus | None = None, limit: int | None = 50
    ) -> list[JournalEntry]:
        """Up to `limit` (None: all) journal entries, newest first, with `status` (None: any)."""
        rows = self._fetch_all(
            "SELECT * FROM journal WHERE CASE ?"
            " WHEN 'pending_lessons' THEN lesson_pending = 1"
            " ELSE ? IS NULL OR status = ? END"
            " ORDER BY id DESC LIMIT ?",
            (status, status, status, -1 if limit is None else limit),
        )
        return [JournalEntry.model_validate(dict(row)) for row in rows]

    # Reviews

    def add_review(
        self,
        created_at: datetime,
        packet_hash: str,
        outcomes: Sequence[ReviewOutcome],
        decision: str,
        final_order: SizedOrder | None,
    ) -> int:
        """Store one cross-model review and return its id.

        `outcomes` holds every reviewer's full outcome; `final_order` is the order
        after the review's changes, or None when the review blocked the buy. A journal
        entry links to the review through its `review_id`.
        """
        return self._insert(
            "INSERT INTO reviews (created_at, packet_hash, outcomes, decision, final_order)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                created_at.isoformat(),
                packet_hash,
                json.dumps([outcome.model_dump(mode="json") for outcome in outcomes]),
                decision,
                final_order.model_dump_json() if final_order else None,
            ),
        )

    def reviews_since(self, since: datetime) -> list[StoredReview]:
        """The reviews stored at or after `since`, in the order they were stored."""
        # Filtered here, not in SQL: SQLite compares times only to the millisecond.
        reviews = [
            StoredReview(
                id=row["id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                packet_hash=row["packet_hash"],
                outcomes=[ReviewOutcome.model_validate(o) for o in json.loads(row["outcomes"])],
                decision=row["decision"],
                final_order=(
                    SizedOrder.model_validate_json(row["final_order"])
                    if row["final_order"]
                    else None
                ),
            )
            for row in self._fetch_all("SELECT * FROM reviews ORDER BY id")
        ]
        return [review for review in reviews if review.created_at >= since]

    # Blocks

    def add_block(
        self,
        at: datetime,
        tool: str,
        symbol: str,
        rules: Sequence[BlockRule],
        reasons: Sequence[str],
    ) -> None:
        """Record a refused tool call: the ids of the rules that refused it, and why.

        A rule id repeated in `rules` is stored once.
        """
        self._execute(
            "INSERT INTO blocks (at, tool, symbol, rules, reasons) VALUES (?, ?, ?, ?, ?)",
            (
                at.isoformat(),
                tool,
                symbol,
                json.dumps(list(dict.fromkeys(rules))),
                json.dumps(list(reasons)),
            ),
        )

    def recent_blocks(self, limit: int = 20) -> list[Block]:
        """Up to `limit` refused tool calls, newest first."""
        rows = self._fetch_all("SELECT * FROM blocks ORDER BY id DESC LIMIT ?", (limit,))
        return [_block(row) for row in rows]

    def blocks_since(self, since: datetime) -> list[Block]:
        """The refused tool calls at or after `since`, in the order they were recorded."""
        # Filtered here, not in SQL: SQLite compares times only to the millisecond.
        blocks = [_block(row) for row in self._fetch_all("SELECT * FROM blocks ORDER BY id")]
        return [block for block in blocks if block.at >= since]

    # Integrity violations

    def record_integrity_violation(
        self, at: datetime, kind: str, subject: str, detail: str
    ) -> bool:
        """Record a violation once per (`kind`, `subject`); return True if it is new.

        Housekeeping sees the same foreign order or position on every run, so a
        repeat of a recorded violation is ignored rather than counted again.
        """
        inserted = self._execute(
            "INSERT OR IGNORE INTO integrity_violations (at, kind, subject, detail)"
            " VALUES (?, ?, ?, ?)",
            (at.isoformat(), kind, subject, detail),
        )
        return inserted == 1

    def integrity_violation_count(self) -> int:
        """Number of distinct integrity violations recorded."""
        row = self._fetch_one("SELECT COUNT(*) AS violations FROM integrity_violations")
        assert row is not None
        return int(row["violations"])

    # SQLite access

    def _validate_schema(self) -> None:
        """Raise StateUnavailableError unless the file holds this version's tables."""
        try:
            tables = {
                row["name"]
                for row in self._fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            row = (
                self._fetch_one("SELECT schema_version FROM account_state")
                if "account_state" in tables
                else None
            )
        except StateUnavailableError as exc:
            raise StateUnavailableError(
                f"state database {self.path} is unreadable: {exc}"
            ) from None
        if not _TABLES.issubset(tables) or row is None:
            raise StateUnavailableError(f"{self.path} is not a trader-broker state database")
        if row["schema_version"] != SCHEMA_VERSION:
            raise StateUnavailableError(
                f"state database {self.path} has schema version {row['schema_version']},"
                f" expected {SCHEMA_VERSION}"
            )

    @contextmanager
    def _sqlite_errors(self) -> Iterator[None]:
        """Report SQLite failures as unavailable state; constraint errors pass through."""
        try:
            yield
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as exc:
            raise StateUnavailableError(str(exc)) from exc

    def _execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """Run one write in its own transaction and return the number of rows changed."""
        with self._sqlite_errors(), self._connection:
            return self._connection.execute(sql, parameters).rowcount

    def _insert(self, sql: str, parameters: Sequence[Any]) -> int:
        """Run one INSERT in its own transaction and return the new row's id."""
        with self._sqlite_errors(), self._connection:
            row_id = self._connection.execute(sql, parameters).lastrowid
        assert row_id is not None
        return row_id

    def _fetch_one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._sqlite_errors():
            row: sqlite3.Row | None = self._cursor(sql, parameters).fetchone()
            return row

    def _fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._sqlite_errors():
            return self._cursor(sql, parameters).fetchall()

    def _cursor(self, sql: str, parameters: Sequence[Any]) -> sqlite3.Cursor:
        cursor = self._connection.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, parameters)


def init_state(
    path: Path, equity: Decimal, force: bool = False, today: date | None = None
) -> StateStore:
    """Create a fresh state database at `path` and return it open.

    The peak starts at `equity`, the kill switch and daily halt are clear, and
    `paper_start_date` is `today` (default: today in New York). Creates missing
    parent directories. Raises FileExistsError when `path` exists, unless `force`,
    which replaces it and discards everything stored there.
    """
    if path.exists():
        if not force:
            raise FileExistsError(
                f"state database {path} already exists; use --force to replace it"
            )
        for leftover in (path, *(path.with_name(path.name + s) for s in ("-journal", "-wal"))):
            leftover.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = today or market_date(datetime.now(UTC))

    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        with connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT INTO account_state (id, schema_version, equity_peak, kill_switch,"
                " daily_halt_date, paper_start_date) VALUES (1, ?, ?, 0, NULL, ?)",
                (SCHEMA_VERSION, str(equity), start.isoformat()),
            )
    finally:
        connection.close()
    return StateStore.open(path)


def _block(row: sqlite3.Row) -> Block:
    return Block(
        at=datetime.fromisoformat(row["at"]),
        tool=row["tool"],
        symbol=row["symbol"],
        rules=json.loads(row["rules"]),
        reasons=json.loads(row["reasons"]),
    )


def _stored_order(row: sqlite3.Row) -> StoredOrder:
    return StoredOrder(
        client_order_id=row["client_order_id"],
        broker_order_id=row["broker_order_id"],
        symbol=row["symbol"],
        side=row["side"],
        role=row["role"],
        status=row["status"],
        sizing=SizedOrder.model_validate_json(row["sizing"]) if row["sizing"] else None,
        proposal=Proposal.model_validate_json(row["proposal"]) if row["proposal"] else None,
        review_id=row["review_id"],
        journal_entry_id=row["journal_entry_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _iso_or_none(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _date_or_none(text: str | None) -> date | None:
    return None if text is None else date.fromisoformat(text)
