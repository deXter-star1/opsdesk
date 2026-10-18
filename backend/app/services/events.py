"""History events. Written in the same transaction as the change they describe, so history
can never disagree with state, and follow-up work (notifications) is enqueued atomically."""
from __future__ import annotations

import sqlite3

from ..db import dumps
from ..jobs import queue
from ..timeutil import now_iso

# Event kinds whose fan-out may notify someone. The handler decides the actual recipients.
NOTIFYING_KINDS = frozenset({"created", "assigned", "status_changed", "commented", "updated", "moved",
                             "sla_breached"})


def record(conn: sqlite3.Connection, item: dict, actor_id: int | None, kind: str, data: dict,
           *, versioned: bool) -> int:
    """`item` must be the row AFTER the change (its team_id/version are recorded)."""
    event_id = conn.execute(
        """INSERT INTO item_events (item_id, team_id, actor_id, kind, item_version, data, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (item["id"], item["team_id"], actor_id, kind, item["version"] if versioned else None, dumps(data),
         now_iso()),
    ).fetchone()["id"]
    if kind in NOTIFYING_KINDS:
        queue.enqueue(conn, "fanout_notifications", {"event_id": event_id})
    return event_id


def add_watcher(conn: sqlite3.Connection, item_id: int, user_id: int | None) -> None:
    if user_id is None:
        return
    conn.execute(
        "INSERT INTO item_watchers (item_id, user_id, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (item_id, user_id, now_iso()),
    )
