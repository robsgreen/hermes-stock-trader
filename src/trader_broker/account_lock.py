"""A lock that lets one process at a time change an account's orders and state.

Several `trader-broker` processes can serve the same account: a `serve` per Hermes
session and the operator's `status`. Each runs housekeeping and the buy and exit tools,
which read the broker, decide and write; two interleaved could both place a stop for
the same shares or both pass a cap. `AccountLock` is an exclusive `flock` on a file
beside the state database, so those calls take turns across processes.
"""

import fcntl
import time
from pathlib import Path
from types import TracebackType
from typing import IO

DEFAULT_TIMEOUT_S = 300.0
"""How long `AccountLock` waits for another process by default, as CLI `status` does:
longer than one buy, reviews included, takes. `serve` waits the shorter
`Settings.account_lock_wait_s`, so a busy call answers within Hermes's call timeout."""

_POLL_INTERVAL_S = 0.1


class AccountBusyError(Exception):
    """Another process held the account lock for longer than the wait allowed."""


class AccountLock:
    """An exclusive, cross-process lock on one account, used as a context manager.

    Entering waits up to `timeout_s` for the lock and raises `AccountBusyError` after;
    leaving releases it. The lock file and its directory are created when missing.
    The operating system releases the lock if the process dies. Not reentrant: enter
    it once per call, and use it from one thread at a time.
    """

    def __init__(self, path: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._file: IO[bytes] | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._path.open("ab")
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    lock_file.close()
                    raise AccountBusyError(
                        f"another trader-broker process held {self._path} for over "
                        f"{self._timeout_s:g} s; try again shortly"
                    ) from None
                time.sleep(_POLL_INTERVAL_S)
            else:
                self._file = lock_file
                return

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._file = None
