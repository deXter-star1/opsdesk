"""Idempotency keys for mutating requests.

The client sends `Idempotency-Key: <uuid>` once per user intent and reuses it on retries
(network drop, timeout, double click). The key row is written in the SAME transaction as
the mutation it protects:

  * first request: INSERT the key (placeholder) -> run the mutation -> store the response -> COMMIT.
  * duplicate after commit: INSERT conflicts -> return the stored response, mutation not repeated.
  * duplicate while the first is still running: on PostgreSQL the INSERT blocks on the unique
    index until the first transaction finishes, then conflicts (-> replay) or succeeds (first one
    rolled back -> run normally). On SQLite writers are serialized so the same outcome follows.
  * first request failed (validation, 409, crash): its transaction rolled back, the key row with
    it, so a retry is evaluated afresh. Only successful outcomes are ever replayed.

Same key with a different method/path/body is a client bug -> 422, never a silent replay.
"""
from __future__ import annotations

import hashlib
import sqlite3

from ..db import dumps, loads
from ..errors import Conflict, Unprocessable
from ..timeutil import now_iso

MAX_KEY_LENGTH = 200


def fingerprint(method: str, path: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\n")
    h.update(path.encode())
    h.update(b"\n")
    h.update(body)
    return h.hexdigest()


def begin(conn: sqlite3.Connection, user_id: int, key: str, fp: str) -> dict | None:
    """Claim `key` for this request. Returns the stored response if it was already used."""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise Unprocessable("Idempotency-Key must be 1-200 characters", code="bad_idempotency_key")
    inserted = conn.execute(
        """INSERT INTO idempotency_keys (user_id, key, fingerprint, status_code, response, created_at)
           VALUES (?, ?, ?, 0, '', ?) ON CONFLICT (user_id, key) DO NOTHING RETURNING key""",
        (user_id, key, fp, now_iso()),
    ).fetchone()
    if inserted:
        return None
    row = conn.execute(
        "SELECT fingerprint, status_code, response FROM idempotency_keys WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()
    if row["fingerprint"] != fp:
        raise Unprocessable("This Idempotency-Key was already used for a different request",
                            code="idempotency_key_reused")
    if row["status_code"] == 0:  # defensive: only reachable on databases without serialized writers
        raise Conflict("The original request is still being processed", code="idempotency_in_progress")
    return {"status": row["status_code"], "body": loads(row["response"])}


def complete(conn: sqlite3.Connection, user_id: int, key: str, status: int, body) -> None:
    conn.execute(
        "UPDATE idempotency_keys SET status_code = ?, response = ? WHERE user_id = ? AND key = ?",
        (status, dumps(body), user_id, key),
    )
