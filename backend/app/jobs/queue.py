"""Durable job queue on top of the `jobs` table (a transactional outbox).

Delivery is AT-LEAST-ONCE. A job can run more than once if a worker dies after doing the
work but before recording completion, or if its lease expires while it is still running.
So every handler must be idempotent (see handlers.py), and completion is *fenced*:

    complete() only succeeds if this worker still holds the lease it was given
    (same lease_owner AND same attempt number). If the lease was lost, completion raises
    LeaseLost, and because the handler's writes are in the same transaction as complete(),
    those writes are rolled back too. A slow, "zombie" worker can never commit over a
    newer attempt.

Portability note: on SQLite the claim UPDATE is atomic because writers are serialized.
On PostgreSQL the inner SELECT would use `FOR UPDATE SKIP LOCKED` so concurrent workers
skip each other's rows instead of blocking.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import timedelta

from ..db import dumps, loads, rows_to_dicts
from ..timeutil import now_iso, to_iso, utcnow


class LeaseLost(Exception):
    """Raised when a worker tries to complete/fail a job it no longer owns."""


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict, *, run_at: str | None = None,
            dedupe_key: str | None = None, max_attempts: int = 5) -> int | None:
    """Insert a job in the caller's transaction. Returns None if dedupe_key already exists."""
    now = now_iso()
    row = conn.execute(
        """INSERT INTO jobs (kind, payload, status, max_attempts, run_at, dedupe_key, created_at, updated_at)
           VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
           ON CONFLICT (dedupe_key) DO NOTHING
           RETURNING id""",
        (kind, dumps(payload), max_attempts, run_at or now, dedupe_key, now, now),
    ).fetchone()
    return row["id"] if row else None


def claim(conn: sqlite3.Connection, worker_id: str, *, limit: int, lease_seconds: int) -> list[dict]:
    """Lease up to `limit` runnable jobs: pending & due, or running with an expired lease."""
    now = now_iso()
    lease_until = to_iso(utcnow() + timedelta(seconds=lease_seconds))
    # A job whose lease keeps expiring (e.g. it crashes the worker) must not loop forever.
    conn.execute(
        """UPDATE jobs SET status = 'dead', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?,
                  last_error = COALESCE(last_error || ' | ', '') || 'lease expired on final attempt'
           WHERE status = 'running' AND lease_expires_at <= ? AND attempts >= max_attempts""",
        (now, now),
    )
    rows = conn.execute(
        """UPDATE jobs
              SET status = 'running', lease_owner = ?, lease_expires_at = ?, attempts = attempts + 1, updated_at = ?
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, run_at FROM jobs WHERE status = 'pending' AND run_at <= ?
                    UNION ALL
                    SELECT id, run_at FROM jobs WHERE status = 'running' AND lease_expires_at <= ?
                ) ORDER BY run_at LIMIT ?)
        RETURNING *""",
        (worker_id, lease_until, now, now, now, limit),
    ).fetchall()
    jobs = rows_to_dicts(rows)
    for j in jobs:
        j["payload"] = loads(j["payload"])
    return jobs


def complete(conn: sqlite3.Connection, job: dict, worker_id: str) -> None:
    cur = conn.execute(
        """UPDATE jobs SET status = 'done', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
           WHERE id = ? AND status = 'running' AND lease_owner = ? AND attempts = ?""",
        (now_iso(), job["id"], worker_id, job["attempts"]),
    )
    if cur.rowcount != 1:
        raise LeaseLost(f"job {job['id']} attempt {job['attempts']} no longer leased by {worker_id}")


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff with jitter: ~2s, 4s, 8s, 16s ... capped at 5 minutes."""
    base = min(300.0, 2.0 ** attempts)
    return base * random.uniform(0.75, 1.25)


def fail(conn: sqlite3.Connection, job: dict, worker_id: str, error: str) -> str:
    """Record a failed attempt. Returns the new status ('pending' for retry, or 'dead')."""
    status = "dead" if job["attempts"] >= job["max_attempts"] else "pending"
    retry_at = to_iso(utcnow() + timedelta(seconds=backoff_seconds(job["attempts"])))
    cur = conn.execute(
        """UPDATE jobs SET status = ?, run_at = ?, last_error = ?, lease_owner = NULL, lease_expires_at = NULL,
                  updated_at = ?
           WHERE id = ? AND status = 'running' AND lease_owner = ? AND attempts = ?""",
        (status, retry_at, error[:2000], now_iso(), job["id"], worker_id, job["attempts"]),
    )
    if cur.rowcount != 1:
        raise LeaseLost(f"job {job['id']} attempt {job['attempts']} no longer leased by {worker_id}")
    return status


def retry_dead(conn: sqlite3.Connection, job_id: int) -> bool:
    cur = conn.execute(
        """UPDATE jobs SET status = 'pending', attempts = 0, run_at = ?, updated_at = ?
           WHERE id = ? AND status = 'dead'""",
        (now_iso(), now_iso(), job_id),
    )
    return cur.rowcount == 1


def stats(conn: sqlite3.Connection) -> dict:
    by_status = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    oldest = conn.execute(
        "SELECT MIN(run_at) AS t FROM jobs WHERE status = 'pending' AND run_at <= ?", (now_iso(),)
    ).fetchone()["t"]
    retrying = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending' AND attempts > 0"
    ).fetchone()["n"]
    return {
        "by_status": {s: by_status.get(s, 0) for s in ("pending", "running", "done", "dead")},
        "retrying": retrying,
        "oldest_ready_at": oldest,
    }


def problem_jobs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT id, kind, payload, status, attempts, max_attempts, run_at, last_error, updated_at
           FROM jobs WHERE status = 'dead' OR (status = 'pending' AND attempts > 0)
           ORDER BY updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = rows_to_dicts(rows)
    for j in out:
        j["payload"] = loads(j["payload"])
    return out
