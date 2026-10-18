"""Row -> API shape. One place so list rows, detail views and conflict payloads all agree."""
from __future__ import annotations

from ..db import loads
from ..timeutil import now_iso

# SELECT fragment that every item query uses, so serializers can rely on these columns.
ITEM_COLUMNS = """
    w.id, w.team_id, w.title, w.kind, w.priority, w.status, w.active, w.assignee_id, w.reporter_id,
    w.requires_approval, w.approval_requested_by, w.approved_by, w.approved_at, w.due_at,
    w.sla_breached_at, w.version, w.created_at, w.updated_at, w.last_activity_at, w.resolved_at,
    t.key AS team_key, t.name AS team_name,
    a.name AS assignee_name, r.name AS reporter_name
"""
ITEM_JOINS = """
    JOIN teams t ON t.id = w.team_id
    LEFT JOIN users a ON a.id = w.assignee_id
    JOIN users r ON r.id = w.reporter_id
"""


def _person(uid, name):
    return {"id": uid, "name": name} if uid is not None else None


def item_summary(row: dict, now: str | None = None) -> dict:
    now = now or now_iso()
    active = row["status"] not in ("resolved", "cancelled")
    return {
        "id": row["id"],
        "team": {"id": row["team_id"], "key": row["team_key"], "name": row["team_name"]},
        "title": row["title"],
        "kind": row["kind"],
        "priority": row["priority"],
        "status": row["status"],
        "assignee": _person(row["assignee_id"], row["assignee_name"]),
        "reporter": _person(row["reporter_id"], row["reporter_name"]),
        "requires_approval": bool(row["requires_approval"]),
        "due_at": row["due_at"],
        "overdue": bool(active and row["due_at"] and row["due_at"] < now),
        "sla_breached": row["sla_breached_at"] is not None,
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_activity_at": row["last_activity_at"],
        "resolved_at": row["resolved_at"],
    }


def event_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "team_id": row["team_id"],
        "kind": row["kind"],
        "actor": _person(row["actor_id"], row.get("actor_name")) if row["actor_id"] else None,
        "item_version": row["item_version"],
        "data": loads(row["data"]),
        "created_at": row["created_at"],
    }
