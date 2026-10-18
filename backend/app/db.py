"""SQLite access: one connection per thread, explicit transactions.

Why explicit transactions: every mutating request runs inside `write_tx()`, which issues
BEGIN IMMEDIATE. The domain change, its history event, any follow-up jobs and the stored
idempotency response are committed together or not at all.

Correctness does NOT depend on SQLite's single-writer lock: every write to a work item is
guarded with `WHERE version = :version_we_read` (see services/items.py), so the same code
stays correct on a database with row-level concurrency such as PostgreSQL. The lock only
means that, on SQLite, the guard almost never has to fire.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

_local = threading.local()


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None => the sqlite3 module never opens transactions implicitly;
    # we issue BEGIN/COMMIT ourselves so transaction boundaries are visible in the code.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")      # readers never block the writer and vice versa
    conn.execute("PRAGMA synchronous = NORMAL")    # durable at checkpoint; safe with WAL
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")    # wait up to 10s for the write lock before failing
    return conn


def get_conn() -> sqlite3.Connection:
    """Thread-local connection for the configured database (re-opened if the path changes)."""
    path = config.settings.database_path
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path:
        if conn is not None:
            conn.close()
        conn = _connect(path)
        _local.conn = conn
        _local.path = path
    return conn


def close_thread_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """A serializable write transaction. Commits on success, rolls back on any exception."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """A read-only snapshot: all queries inside see the same committed state (WAL snapshot)."""
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None) -> Any:
    return json.loads(value) if value else {}
