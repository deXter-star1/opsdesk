"""Read side: item detail, filtered/searchable lists with keyset pagination, dashboard, history.

Scale notes
  * Lists never use OFFSET. They use keyset ("seek") pagination on the sort columns plus id,
    so page 500 costs the same as page 1 and rows don't shift between pages when new items
    are inserted at the top.
  * Counts are capped (COUNT over a LIMITed subquery): "999+" is as useful as an exact number
    for a dashboard tile and cannot turn into a full scan of a large table.
  * Every query is bounded by `limit`; nothing loads the whole dataset into memory.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from .. import config
from ..domain import policy
from ..domain.policy import UserCtx
from ..domain.workflow import STATUSES
from ..errors import NotFound, Unprocessable
from ..timeutil import now_iso, to_iso, utcnow
from .serialize import ITEM_COLUMNS, ITEM_JOINS, event_out, item_summary

COUNT_CAP = 1000

SORTS: dict[str, list[tuple[str, str]]] = {
    "activity": [("w.last_activity_at", "desc"), ("w.id", "desc")],
    "created": [("w.created_at", "desc"), ("w.id", "desc")],
    "priority": [("w.priority", "asc"), ("w.last_activity_at", "desc"), ("w.id", "desc")],
    "due": [("COALESCE(w.due_at, '9999-12-31')", "asc"), ("w.id", "asc")],
}

VIEWS = ("my_work", "awaiting_my_approval", "unassigned_urgent", "overdue", "stale", "my_requests", "watching")


@dataclass
class ItemFilters:
    q: str | None = None
    view: str | None = None
    team_ids: list[int] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    priorities: list[int] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    assignee: str | None = None      # "me" | "none" | "<user id>"
    state: str = "active"             # "active" | "closed" | "all" (ignored when statuses given)
    sort: str = "activity"
    cursor: str | None = None
    limit: int = 25


# --------------------------------------------------------------------------- detail

def _fetch_item_row(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(f"SELECT {ITEM_COLUMNS}, w.description, w.resolution FROM work_items w {ITEM_JOINS} "
                       "WHERE w.id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def item_detail(conn: sqlite3.Connection, user: UserCtx, item_id: int) -> dict:
    row = policy.require_view(user, _fetch_item_row(conn, item_id))
    out = item_summary(row)
    names = {}
    ids = [i for i in (row["approval_requested_by"], row["approved_by"]) if i]
    if ids:
        names = {r["id"]: r["name"] for r in conn.execute(
            f"SELECT id, name FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)}
    watch = conn.execute(
        "SELECT COUNT(*) AS n, SUM(user_id = ?) AS mine FROM item_watchers WHERE item_id = ?", (user.id, item_id)
    ).fetchone()
    out.update({
        "description": row["description"],
        "resolution": row["resolution"],
        "approval_requested_by": ({"id": row["approval_requested_by"], "name": names.get(row["approval_requested_by"])}
                                  if row["approval_requested_by"] else None),
        "approved_by": ({"id": row["approved_by"], "name": names.get(row["approved_by"])}
                        if row["approved_by"] else None),
        "approved_at": row["approved_at"],
        "watcher_count": watch["n"],
        "watching": bool(watch["mine"]),
        "my_role": user.roles.get(row["team_id"]),
        "allowed_actions": policy.allowed_actions(user, row),
    })
    return out


# --------------------------------------------------------------------------- lists

def _encode_cursor(sort: str, values: list) -> str:
    return base64.urlsafe_b64encode(json.dumps([sort, values]).encode()).decode()


def _decode_cursor(sort: str, cursor: str) -> list:
    try:
        cur_sort, values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        raise Unprocessable("Malformed cursor", code="bad_cursor") from None
    if cur_sort != sort or len(values) != len(SORTS[sort]):
        raise Unprocessable("Cursor does not match the requested sort", code="bad_cursor")
    return values


def keyset_predicate(columns: list[tuple[str, str]], values: list) -> tuple[str, list]:
    """Rows strictly after `values` in the given ordering, supporting mixed ASC/DESC columns:
       (c1 > v1) OR (c1 = v1 AND c2 < v2) OR (c1 = v1 AND c2 = v2 AND c3 < v3) ..."""
    ors, params = [], []
    for i, (expr, direction) in enumerate(columns):
        parts = [f"{columns[j][0]} = ?" for j in range(i)]
        parts.append(f"{expr} {'>' if direction == 'asc' else '<'} ?")
        ors.append("(" + " AND ".join(parts) + ")")
        params.extend(values[: i + 1])
    return "(" + " OR ".join(ors) + ")", params


def fts_query(q: str) -> str | None:
    """Turn user input into a safe FTS5 query: each word becomes a quoted prefix term, ANDed.
    Quoting every token means FTS operators in user input (NEAR, -, ", *) are inert."""
    tokens = re.findall(r"\w+", q.lower())[:8]
    if not tokens:
        return None
    return " AND ".join(f'"{t}"*' for t in tokens)


def _where(conn: sqlite3.Connection, user: UserCtx, f: ItemFilters) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    now = now_iso()

    # Visibility first: this clause is what makes list/search/dashboard respect authorization.
    if user.team_ids:
        where.append(f"(w.team_id IN ({','.join('?' * len(user.team_ids))}) OR w.reporter_id = ?)")
        params += user.team_ids + [user.id]
    else:
        where.append("w.reporter_id = ?")
        params.append(user.id)

    if f.statuses:
        where.append(f"w.status IN ({','.join('?' * len(f.statuses))})")
        params += f.statuses
    elif f.state == "active":
        where.append("w.active = 1")
    elif f.state == "closed":
        where.append("w.active = 0")

    if f.team_ids:
        where.append(f"w.team_id IN ({','.join('?' * len(f.team_ids))})")
        params += f.team_ids
    if f.priorities:
        where.append(f"w.priority IN ({','.join('?' * len(f.priorities))})")
        params += f.priorities
    if f.kinds:
        where.append(f"w.kind IN ({','.join('?' * len(f.kinds))})")
        params += f.kinds
    if f.assignee == "me":
        where.append("w.assignee_id = ?")
        params.append(user.id)
    elif f.assignee == "none":
        where.append("w.assignee_id IS NULL")
    elif f.assignee:
        where.append("w.assignee_id = ?")
        params.append(int(f.assignee))

    if f.q:
        q = f.q.strip()
        if re.fullmatch(r"#?\d+", q):
            where.append("w.id = ?")
            params.append(int(q.lstrip("#")))
        else:
            match = fts_query(q)
            if match:
                where.append("w.id IN (SELECT rowid FROM work_items_fts WHERE work_items_fts MATCH ?)")
                params.append(match)

    v = f.view
    if v == "my_work":
        where += ["w.assignee_id = ?", "w.active = 1"]
        params.append(user.id)
    elif v == "awaiting_my_approval":
        leads = user.lead_team_ids()
        if not leads:
            where.append("0")
        else:
            where.append(f"w.team_id IN ({','.join('?' * len(leads))})")
            params += leads
            where += ["w.status = 'awaiting_approval'", "COALESCE(w.assignee_id, 0) != ?",
                      "COALESCE(w.approval_requested_by, 0) != ?"]
            params += [user.id, user.id]
    elif v == "unassigned_urgent":
        teams = [t for t in user.team_ids if user.rank(t) >= policy.MEMBER]
        if not teams:
            where.append("0")
        else:
            where.append(f"w.team_id IN ({','.join('?' * len(teams))})")
            params += teams
            where += ["w.active = 1", "w.assignee_id IS NULL", "w.priority <= 2"]
    elif v == "overdue":
        where += ["w.active = 1", "w.due_at IS NOT NULL", "w.due_at < ?"]
        params.append(now)
    elif v == "stale":
        cutoff = to_iso(utcnow() - timedelta(hours=config.settings.stale_after_hours))
        where += ["w.active = 1", "w.last_activity_at < ?"]
        params.append(cutoff)
    elif v == "my_requests":
        where += ["w.reporter_id = ?", "w.active = 1"]
        params.append(user.id)
    elif v == "watching":
        where.append("w.id IN (SELECT item_id FROM item_watchers WHERE user_id = ?)")
        params.append(user.id)
    return where, params


def validate_filters(f: ItemFilters) -> ItemFilters:
    if f.sort not in SORTS:
        raise Unprocessable(f"sort must be one of {sorted(SORTS)}", code="bad_sort")
    if f.view and f.view not in VIEWS:
        raise Unprocessable(f"view must be one of {list(VIEWS)}", code="bad_view")
    if f.state not in ("active", "closed", "all"):
        raise Unprocessable("state must be active, closed or all", code="bad_state")
    bad = [s for s in f.statuses if s not in STATUSES]
    if bad:
        raise Unprocessable(f"unknown status {bad}", code="bad_status")
    if f.assignee not in (None, "me", "none") and not str(f.assignee).isdigit():
        raise Unprocessable("assignee must be me, none or a user id", code="bad_assignee")
    f.limit = max(1, min(100, f.limit))
    return f


def list_items(conn: sqlite3.Connection, user: UserCtx, f: ItemFilters) -> dict:
    f = validate_filters(f)
    where, params = _where(conn, user, f)
    order_cols = SORTS[f.sort]
    if f.cursor:
        pred, pparams = keyset_predicate(order_cols, _decode_cursor(f.sort, f.cursor))
        where.append(pred)
        params += pparams
    order_sql = ", ".join(f"{expr} {d.upper()}" for expr, d in order_cols)
    sort_select = ", ".join(f"{expr} AS _s{i}" for i, (expr, _) in enumerate(order_cols))
    sql = (f"SELECT {ITEM_COLUMNS}, {sort_select} FROM work_items w {ITEM_JOINS} "
           f"WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?")
    rows = [dict(r) for r in conn.execute(sql, params + [f.limit + 1])]
    has_more = len(rows) > f.limit
    rows = rows[: f.limit]
    now = now_iso()
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = _encode_cursor(f.sort, [last[f"_s{i}"] for i in range(len(order_cols))])
    return {"items": [item_summary(r, now) for r in rows], "next_cursor": next_cursor}


def count_items(conn: sqlite3.Connection, user: UserCtx, f: ItemFilters) -> dict:
    where, params = _where(conn, user, validate_filters(f))
    n = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM work_items w WHERE {' AND '.join(where)} LIMIT ?)",
        params + [COUNT_CAP],
    ).fetchone()["n"]
    return {"count": n, "capped": n >= COUNT_CAP}


# --------------------------------------------------------------------------- dashboard

DASHBOARD_TILES = [
    ("my_work", "priority"),
    ("awaiting_my_approval", "activity"),
    ("unassigned_urgent", "priority"),
    ("overdue", "due"),
    ("stale", "activity"),
    ("my_requests", "activity"),
]


def dashboard(conn: sqlite3.Connection, user: UserCtx) -> dict:
    tiles = {}
    for view, sort in DASHBOARD_TILES:
        f = ItemFilters(view=view, sort=sort, limit=5)
        tiles[view] = {**count_items(conn, user, f), "items": list_items(conn, user, f)["items"]}
    return {"tiles": tiles, "recent_activity": recent_activity(conn, user, limit=15)}


def recent_activity(conn: sqlite3.Connection, user: UserCtx, limit: int = 20) -> list[dict]:
    if not user.team_ids:
        return []
    rows = conn.execute(
        f"""SELECT e.*, u.name AS actor_name, w.title AS item_title
            FROM item_events e
            JOIN work_items w ON w.id = e.item_id
            LEFT JOIN users u ON u.id = e.actor_id
            WHERE e.team_id IN ({','.join('?' * len(user.team_ids))})
            ORDER BY e.id DESC LIMIT ?""",
        user.team_ids + [limit],
    ).fetchall()
    out = []
    for r in rows:
        ev = event_out(dict(r))
        ev["item_title"] = r["item_title"]
        out.append(ev)
    return out


# --------------------------------------------------------------------------- history

def item_events(conn: sqlite3.Connection, user: UserCtx, item_id: int, before_id: int | None,
                limit: int = 50) -> dict:
    policy.require_view(user, _fetch_item_row(conn, item_id))
    limit = max(1, min(200, limit))
    params: list = [item_id]
    extra = ""
    if before_id:
        extra = "AND e.id < ?"
        params.append(before_id)
    rows = conn.execute(
        f"""SELECT e.*, u.name AS actor_name FROM item_events e LEFT JOIN users u ON u.id = e.actor_id
            WHERE e.item_id = ? {extra} ORDER BY e.id DESC LIMIT ?""",
        params + [limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"events": [event_out(dict(r)) for r in rows],
            "next_before": rows[-1]["id"] if has_more and rows else None}


# --------------------------------------------------------------------------- teams

def list_teams(conn: sqlite3.Connection, user: UserCtx) -> list[dict]:
    rows = conn.execute("SELECT id, key, name, description FROM teams ORDER BY name").fetchall()
    return [{**dict(r), "my_role": user.roles.get(r["id"])} for r in rows]


def team_members(conn: sqlite3.Connection, user: UserCtx, team_id: int) -> list[dict]:
    if user.rank(team_id) < policy.VIEWER:
        raise NotFound("Team not found")
    rows = conn.execute(
        """SELECT u.id, u.name, u.email, m.role FROM memberships m JOIN users u ON u.id = m.user_id
           WHERE m.team_id = ? ORDER BY CASE m.role WHEN 'lead' THEN 0 WHEN 'member' THEN 1 ELSE 2 END, u.name
           LIMIT 500""",
        (team_id,),
    ).fetchall()
    return [dict(r) for r in rows]
