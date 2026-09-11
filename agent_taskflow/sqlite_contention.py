"""SQLite lock-contention observability (V1 Step 4, SPEC §19.2).

``store.connect()`` opens every connection with
:class:`ContentionObservingConnection`. The connection behaves exactly like a
plain ``sqlite3.Connection``; it additionally measures two things:

``busy wait``
    an explicit ``BEGIN IMMEDIATE`` / ``BEGIN EXCLUSIVE`` that did not get the
    write lock within :data:`BUSY_WAIT_THRESHOLD_SECONDS`. Uncontended, those
    statements return in microseconds; SQLite's busy handler sleeps at least
    one millisecond before its first retry, so a slower acquisition means the
    connection waited for another writer. Every lifecycle writer in this
    repository (claim, heartbeat, release, reaper, reset, ObservedStep)
    acquires its lock this way.

``busy timeout``
    any statement that failed with ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` after
    ``busy_timeout`` ran out. The error is still raised.

Implicit deferred write transactions (a bare ``INSERT``/``UPDATE`` inside
``with conn:``) are observed only when they time out: their lock wait cannot be
separated from the statement's own execution time.

Counters are process-local and thread-safe. Each event is also logged on
:data:`CONTENTION_LOGGER_NAME` as ``"<event> <json>"``: busy waits at INFO,
timeouts at WARNING. Nothing is written to the database.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any

CONTENTION_LOGGER_NAME = "agent_taskflow.sqlite_contention"
BUSY_WAIT_THRESHOLD_SECONDS = 0.001

_logger = logging.getLogger(CONTENTION_LOGGER_NAME)
_LOCKING_BEGIN = re.compile(r"^\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", re.IGNORECASE)
_BUSY_ERROR_NAMES = ("SQLITE_BUSY", "SQLITE_LOCKED")
_BUSY_MESSAGES = ("database is locked", "database table is locked", "database is busy")

_counter_lock = threading.Lock()
_counters: dict[str, float] = {}


def _zero_counters() -> dict[str, float]:
    return {
        "lock_acquisitions": 0,
        "busy_waits": 0,
        "busy_wait_seconds_total": 0.0,
        "busy_wait_seconds_max": 0.0,
        "busy_timeouts": 0,
    }


_counters.update(_zero_counters())


def contention_snapshot() -> dict[str, Any]:
    """Return this process's contention counters."""
    with _counter_lock:
        return dict(_counters)


def reset_contention_counters() -> None:
    """Zero this process's contention counters."""
    with _counter_lock:
        _counters.update(_zero_counters())


def _statement_label(sql: str) -> str:
    return " ".join(sql.split())[:120]


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    name = getattr(exc, "sqlite_errorname", None) or ""
    if name.startswith(_BUSY_ERROR_NAMES):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _BUSY_MESSAGES)


def _log(level: int, event: str, payload: dict[str, Any]) -> None:
    body = {
        "event": event,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        **payload,
    }
    _logger.log(level, "%s %s", event, json.dumps(body, sort_keys=True))


class ContentionObservingConnection(sqlite3.Connection):
    """``sqlite3.Connection`` that counts and logs lock waits and timeouts."""

    def __init__(self, database: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(database, *args, **kwargs)
        self._contention_db_path = str(database)

    def _record_busy_timeout(self, sql: str, exc: sqlite3.OperationalError) -> None:
        with _counter_lock:
            _counters["busy_timeouts"] += 1
        _log(
            logging.WARNING,
            "sqlite_busy_timeout",
            {
                "db_path": self._contention_db_path,
                "statement": _statement_label(sql),
                "error": str(exc),
            },
        )

    def _record_lock_acquisition(self, sql: str, waited: float) -> None:
        busy = waited >= BUSY_WAIT_THRESHOLD_SECONDS
        with _counter_lock:
            _counters["lock_acquisitions"] += 1
            if busy:
                _counters["busy_waits"] += 1
                _counters["busy_wait_seconds_total"] += waited
                _counters["busy_wait_seconds_max"] = max(
                    _counters["busy_wait_seconds_max"], waited
                )
        if busy:
            _log(
                logging.INFO,
                "sqlite_busy_wait",
                {
                    "db_path": self._contention_db_path,
                    "statement": _statement_label(sql),
                    "waited_seconds": round(waited, 6),
                },
            )

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        locking = bool(_LOCKING_BEGIN.match(sql))
        started = time.perf_counter()
        try:
            cursor = super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc):
                self._record_busy_timeout(sql, exc)
            raise
        if locking:
            self._record_lock_acquisition(sql, time.perf_counter() - started)
        return cursor

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        try:
            return super().executemany(sql, parameters)
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc):
                self._record_busy_timeout(sql, exc)
            raise

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        try:
            return super().executescript(sql_script)
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc):
                self._record_busy_timeout(sql_script, exc)
            raise


__all__ = [
    "BUSY_WAIT_THRESHOLD_SECONDS",
    "CONTENTION_LOGGER_NAME",
    "ContentionObservingConnection",
    "contention_snapshot",
    "reset_contention_counters",
]
