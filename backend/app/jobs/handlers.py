"""Job handlers. Each runs inside the worker's transaction together with queue.complete(),
so for database-only effects a job's work and its completion commit atomically.

Every handler must be safe to execute more than once (delivery is at-least-once):
  * fanout_notifications -> UNIQUE(event_id, user_id) + ON CONFLICT DO NOTHING
  * sla_sweep            -> conditional UPDATE ... WHERE sla_breached_at IS NULL claims each breach once
  * maintenance          -> deletes are naturally idempotent
"""
from __future__ import annotations

import random
import sqlite3
from datetime import timedelta

from .. import config
from ..services import events, notifications
from ..timeutil import now_iso, to_iso, utcnow

SLA_BATCH = 200


class InjectedFailure(RuntimeError):
    """Raised on purpose when OPSDESK_JOB_FAILURE_RATE > 0, to demonstrate retries and dead-lettering."""


def _maybe_fail(kind: str) -> None:
    rate = config.settings.job_failure_rate
    if rate > 0 and random.random() < rate:
        raise InjectedFailure(f"injected failure in {kind} (OPSDESK_JOB_FAILURE_RATE={rate})")


def fanout_notifications(conn: sqlite3.Connection, payload: dict) -> dict:
    _maybe_fail("fanout_notifications")
    return {"created": notifications.fanout(conn, int(payload["event_id"]))}


def sla_sweep(conn: sqlite3.Connection, payload: dict) -> dict:
    """Flag active items whose deadline passed and notify owner + leads. Bounded batch per run;
    anything left over is picked up by the next scheduled run."""
    _maybe_fail("sla_sweep")
    now = now_iso()
    candidates = conn.execute(
        """SELECT id FROM work_items
           WHERE active = 1 AND sla_breached_at IS NULL AND due_at IS NOT NULL AND due_at < ?
           ORDER BY due_at LIMIT ?""",
        (now, SLA_BATCH),
    ).fetchall()
    flagged = 0
    for c in candidates:
        # The WHERE re-checks the condition at write time, so two sweeps can never both flag an item.
        row = conn.execute(
            """UPDATE work_items SET sla_breached_at = ?
               WHERE id = ? AND active = 1 AND sla_breached_at IS NULL AND due_at < ? RETURNING *""",
            (now, c["id"], now),
        ).fetchone()
        if row is None:
            continue
        item = dict(row)
        events.record(conn, item, None, "sla_breached", {"due_at": item["due_at"]}, versioned=False)
        flagged += 1
    return {"flagged": flagged}


def maintenance(conn: sqlite3.Connection, payload: dict) -> dict:
    now = utcnow()
    keys = conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?",
                        (to_iso(now - timedelta(hours=24)),)).rowcount
    sessions = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),)).rowcount
    jobs = conn.execute("DELETE FROM jobs WHERE status = 'done' AND updated_at < ?",
                        (to_iso(now - timedelta(days=7)),)).rowcount
    return {"idempotency_keys": keys, "sessions": sessions, "jobs": jobs}


HANDLERS = {
    "fanout_notifications": fanout_notifications,
    "sla_sweep": sla_sweep,
    "maintenance": maintenance,
}
