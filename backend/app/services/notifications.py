"""In-app notifications: read side + the idempotent fan-out used by the worker."""
from __future__ import annotations

import sqlite3

from ..db import loads
from ..domain import policy
from ..domain.policy import UserCtx
from ..timeutil import now_iso


def list_notifications(conn: sqlite3.Connection, user: UserCtx, before_id: int | None, unread_only: bool,
                       limit: int = 30) -> dict:
    limit = max(1, min(100, limit))
    where, params = ["n.user_id = ?"], [user.id]
    if unread_only:
        where.append("n.read_at IS NULL")
    if before_id:
        where.append("n.id < ?")
        params.append(before_id)
    rows = conn.execute(
        f"""SELECT n.id, n.item_id, n.reason, n.created_at, n.read_at,
                   w.title AS item_title, w.status AS item_status, w.team_id, w.reporter_id,
                   e.kind AS event_kind, e.data AS event_data, u.name AS actor_name
            FROM notifications n
            JOIN work_items w ON w.id = n.item_id
            JOIN item_events e ON e.id = n.event_id
            LEFT JOIN users u ON u.id = e.actor_id
            WHERE {' AND '.join(where)} ORDER BY n.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    out = []
    for r in rows[:limit]:
        # Visibility is re-checked at read time: if you left the team, its notifications vanish.
        if not policy.can_view(user, {"team_id": r["team_id"], "reporter_id": r["reporter_id"]}):
            continue
        out.append({
            "id": r["id"], "item_id": r["item_id"], "reason": r["reason"], "created_at": r["created_at"],
            "read": r["read_at"] is not None, "item_title": r["item_title"], "item_status": r["item_status"],
            "event_kind": r["event_kind"], "event_data": loads(r["event_data"]), "actor_name": r["actor_name"],
        })
    return {"notifications": out, "next_before": rows[limit - 1]["id"] if has_more else None}


def unread_count(conn: sqlite3.Connection, user: UserCtx) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM notifications WHERE user_id = ? AND read_at IS NULL LIMIT 100)",
        (user.id,),
    ).fetchone()["n"]


def mark_read(conn: sqlite3.Connection, user: UserCtx, ids: list[int], all_: bool) -> dict:
    now = now_iso()
    if all_:
        cur = conn.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
                           (now, user.id))
    elif ids:
        cur = conn.execute(
            f"UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL "
            f"AND id IN ({','.join('?' * len(ids))})",
            [now, user.id] + ids,
        )
    else:
        return {"updated": 0, "unread": unread_count(conn, user)}
    return {"updated": cur.rowcount, "unread": unread_count(conn, user)}


# --------------------------------------------------------------------------- fan-out (worker side)

def _team_leads(conn, team_id: int) -> list[int]:
    return [r["user_id"] for r in
            conn.execute("SELECT user_id FROM memberships WHERE team_id = ? AND role = 'lead'", (team_id,))]


def _watchers(conn, item_id: int) -> list[int]:
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM item_watchers WHERE item_id = ?", (item_id,))]


def recipients_for(conn: sqlite3.Connection, event: dict, item: dict) -> dict[int, str]:
    """Who should hear about this event, and why. First matching reason wins per user."""
    data = loads(event["data"])
    actor = event["actor_id"]
    out: dict[int, str] = {}

    def add(uid, reason):
        if uid and uid != actor and uid not in out:
            out[uid] = reason

    kind = event["kind"]
    if kind == "created":
        if item["priority"] <= 2:
            for uid in _team_leads(conn, item["team_id"]):
                add(uid, "urgent_item_created")
    elif kind == "assigned":
        add(data.get("to"), "assigned_to_you")
        add(data.get("from"), "unassigned_from_you")
        add(item["reporter_id"], "ownership_changed")
    elif kind == "status_changed":
        if data.get("to") == "awaiting_approval":
            for uid in _team_leads(conn, item["team_id"]):
                if uid not in (item["assignee_id"], item["approval_requested_by"]):
                    add(uid, "approval_requested")
        if data.get("action") in ("approve", "reject"):
            add(item["assignee_id"], "approval_decided")
            add(data.get("requested_by"), "approval_decided")
        for uid in _watchers(conn, item["id"]):
            add(uid, "status_changed")
    elif kind == "commented":
        for uid in _watchers(conn, item["id"]):
            add(uid, "new_comment")
    elif kind == "updated":
        prio = data.get("changes", {}).get("priority")
        if prio and prio[1] == 1:
            for uid in _team_leads(conn, item["team_id"]) + _watchers(conn, item["id"]):
                add(uid, "escalated")
    elif kind == "moved":
        for uid in _team_leads(conn, item["team_id"]):
            add(uid, "moved_to_your_team")
        add(item["reporter_id"], "ownership_changed")
    elif kind == "sla_breached":
        add(item["assignee_id"], "sla_breached")
        for uid in _team_leads(conn, item["team_id"]):
            add(uid, "sla_breached")

    if not out:
        return out
    # Only notify people who can (still) see the item.
    members = {r["user_id"] for r in conn.execute(
        f"SELECT user_id FROM memberships WHERE team_id = ? AND user_id IN ({','.join('?' * len(out))})",
        [item["team_id"]] + list(out))}
    return {uid: reason for uid, reason in out.items() if uid in members or uid == item["reporter_id"]}


def fanout(conn: sqlite3.Connection, event_id: int) -> int:
    """Create notifications for one event. Safe to run any number of times: UNIQUE(event_id, user_id)
    plus ON CONFLICT DO NOTHING means a re-run inserts nothing new."""
    ev = conn.execute("SELECT * FROM item_events WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        return 0
    item = dict(conn.execute("SELECT * FROM work_items WHERE id = ?", (ev["item_id"],)).fetchone())
    created = 0
    now = now_iso()
    for uid, reason in recipients_for(conn, dict(ev), item).items():
        cur = conn.execute(
            """INSERT INTO notifications (user_id, event_id, item_id, reason, created_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT (event_id, user_id) DO NOTHING""",
            (uid, event_id, item["id"], reason, now),
        )
        created += cur.rowcount
    return created
