"""Write side: every command that changes a work item.

Every command follows the same shape, inside ONE transaction opened by the caller:

    1. read the current row
    2. authorize + validate against THAT row        (policy.check_*)
    3. optionally compare the client's expected_version with the current version
    4. write with a version guard:  UPDATE ... WHERE id = ? AND version = <version read in 1>
    5. append a history event (+ enqueue notification fan-out) in the same transaction

Step 4 is what makes the read-check-write sequence safe under concurrency on any database:
if another transaction changed the row between steps 1 and 4, the UPDATE matches zero rows
and we refuse instead of overwriting (raise ConcurrentModification). On SQLite the caller's
BEGIN IMMEDIATE already serializes writers, so in practice step 4 never fires there; on
PostgreSQL under READ COMMITTED it is the line of defence.
"""
from __future__ import annotations

import sqlite3

from .. import config
from ..domain import policy
from ..domain.policy import UserCtx
from ..domain.workflow import APPROVAL_BY_DEFAULT, TRANSITIONS
from ..errors import Conflict, Forbidden, Unprocessable
from ..timeutil import iso_in, normalize, now_iso
from . import events, queries
from .serialize import event_out

EDITABLE_FIELDS = ("title", "description", "priority", "kind", "due_at", "requires_approval")


# --------------------------------------------------------------------------- helpers

def _load(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def _load_visible(conn: sqlite3.Connection, user: UserCtx, item_id: int) -> dict:
    return policy.require_view(user, _load(conn, item_id))


def _check_expected_version(conn, user, item: dict, expected_version: int | None) -> None:
    """For commands whose meaning depends on what the user saw (status changes, reassignment):
    if the item changed since the user loaded it, refuse and return the current state."""
    if expected_version is not None and expected_version != item["version"]:
        raise Conflict(
            "This item changed since you loaded it. Review the latest version and try again.",
            code="stale_version",
            details={
                "expected_version": expected_version,
                "current_version": item["version"],
                "item": queries.item_detail(conn, user, item["id"]),
                "changes_since": _events_since(conn, item["id"], expected_version),
            },
        )


def _guarded_update(conn: sqlite3.Connection, item: dict, changes: dict, *, bump_version: bool = True) -> dict:
    """Apply `changes` only if the row still has the version we read. Returns the new row."""
    now = now_iso()
    sets = dict(changes)
    sets["last_activity_at"] = now
    if bump_version:
        sets["updated_at"] = now
    assignments = ", ".join(f"{k} = ?" for k in sets)
    if bump_version:
        assignments += ", version = version + 1"
    row = conn.execute(
        f"UPDATE work_items SET {assignments} WHERE id = ? AND version = ? RETURNING *",
        list(sets.values()) + [item["id"], item["version"]],
    ).fetchone()
    if row is None:
        raise Conflict("The item was modified concurrently; reload and retry", code="concurrent_modification")
    return dict(row)


def _events_since(conn: sqlite3.Connection, item_id: int, version: int) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, u.name AS actor_name FROM item_events e LEFT JOIN users u ON u.id = e.actor_id
           WHERE e.item_id = ? AND e.item_version > ? ORDER BY e.id""",
        (item_id, version),
    ).fetchall()
    return [event_out(dict(r)) for r in rows]


def _fields_touched(evs: list[dict]) -> set[str]:
    touched: set[str] = set()
    for e in evs:
        if e["kind"] == "updated":
            touched |= set(e["data"].get("changes", {}))
        elif e["kind"] == "status_changed":
            touched.add("status")
        elif e["kind"] == "assigned":
            touched.add("assignee")
        elif e["kind"] == "moved":
            touched.add("team")
    return touched


def _team_exists(conn, team_id: int) -> dict:
    row = conn.execute("SELECT id, key, name FROM teams WHERE id = ?", (team_id,)).fetchone()
    if row is None:
        raise Unprocessable("Unknown team", code="unknown_team")
    return dict(row)


# --------------------------------------------------------------------------- create

def create_item(conn: sqlite3.Connection, user: UserCtx, data: dict) -> dict:
    """Anyone signed in may file a request into any team's queue; they become its reporter."""
    team = _team_exists(conn, data["team_id"])
    kind = data["kind"]
    default_approval = kind in APPROVAL_BY_DEFAULT
    requires_approval = data.get("requires_approval")
    if requires_approval is None:
        requires_approval = default_approval
    if default_approval and not requires_approval and user.rank(team["id"]) < policy.LEAD:
        # Non-leads may add an approval step but not remove a default one.
        raise Forbidden("Only a team lead can waive approval for this kind of work", code="lead_required")
    priority = data["priority"]
    due_at = normalize(data.get("due_at")) or iso_in(hours=config.settings.sla_hours_by_priority[priority])
    now = now_iso()
    row = conn.execute(
        """INSERT INTO work_items (team_id, title, description, kind, priority, status, reporter_id,
                                   requires_approval, due_at, version, created_at, updated_at, last_activity_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, 1, ?, ?, ?) RETURNING *""",
        (team["id"], data["title"].strip(), data.get("description", "").strip(), kind, priority, user.id,
         int(requires_approval), due_at, now, now, now),
    ).fetchone()
    item = dict(row)
    events.record(conn, item, user.id, "created",
                  {"title": item["title"], "priority": priority, "kind": kind}, versioned=True)
    events.add_watcher(conn, item["id"], user.id)
    return queries.item_detail(conn, user, item["id"])


# --------------------------------------------------------------------------- edit fields

def update_item(conn: sqlite3.Connection, user: UserCtx, item_id: int, expected_version: int,
                patch: dict) -> dict:
    """Field edits with automatic merge of non-overlapping concurrent edits.

    If the item changed since `expected_version` but none of the fields this request touches
    were changed in between, the edit is applied on top of the current version (merged=True).
    If any field overlaps, nothing is written and 409 edit_conflict returns both sides so the
    client can let the user decide.
    """
    item = _load_visible(conn, user, item_id)
    unknown = set(patch) - set(EDITABLE_FIELDS)
    if unknown:
        raise Unprocessable(f"Fields not editable: {sorted(unknown)}", code="not_editable")
    if "due_at" in patch:
        patch["due_at"] = normalize(patch["due_at"])
    if "requires_approval" in patch:
        patch["requires_approval"] = int(bool(patch["requires_approval"]))
    if "title" in patch:
        patch["title"] = patch["title"].strip()
    policy.check_edit(user, item, set(patch))

    merged = False
    if expected_version != item["version"]:
        since = _events_since(conn, item_id, expected_version)
        overlap = sorted(set(patch) & _fields_touched(since))
        if expected_version > item["version"] or overlap:
            raise Conflict(
                "Someone else changed the same fields while you were editing",
                code="edit_conflict",
                details={
                    "conflicting_fields": overlap,
                    "expected_version": expected_version,
                    "current_version": item["version"],
                    "your_changes": patch,
                    "item": queries.item_detail(conn, user, item_id),
                    "changes_since": since,
                },
            )
        merged = True

    effective = {k: v for k, v in patch.items() if item[k] != v}
    if not effective:
        return {**queries.item_detail(conn, user, item_id), "merged": merged}
    if "due_at" in effective:
        effective["sla_breached_at"] = None  # a new deadline gets a fresh SLA evaluation
    new = _guarded_update(conn, item, effective)
    diff = {k: [item[k], new[k]] for k in patch if k in effective}
    data = {"changes": diff}
    if merged:
        data["merged_over_version"] = expected_version
    events.record(conn, new, user.id, "updated", data, versioned=True)
    return {**queries.item_detail(conn, user, item_id), "merged": merged}


# --------------------------------------------------------------------------- ownership

def claim(conn: sqlite3.Connection, user: UserCtx, item_id: int) -> dict:
    """Take responsibility for an unowned item and start it. Exactly one concurrent claimer wins;
    the others get 409 already_assigned with the winner. Claiming your own item is a no-op."""
    item = _load_visible(conn, user, item_id)
    try:
        policy.check_claim(user, item)
    except Conflict as exc:
        if exc.code == "already_assigned":
            exc.details["item"] = queries.item_detail(conn, user, item_id)
        raise
    if item["assignee_id"] == user.id:
        return queries.item_detail(conn, user, item_id)
    new = _guarded_update(conn, item, {"assignee_id": user.id, "status": "in_progress"})
    events.record(conn, new, user.id, "assigned",
                  {"from": None, "to": user.id, "to_name": user.name, "via": "claim",
                   "status": [item["status"], new["status"]]}, versioned=True)
    events.add_watcher(conn, item_id, user.id)
    return queries.item_detail(conn, user, item_id)


def assign(conn: sqlite3.Connection, user: UserCtx, item_id: int, target_id: int | None,
           expected_version: int | None) -> dict:
    item = _load_visible(conn, user, item_id)
    _check_expected_version(conn, user, item, expected_version)
    target_rank, target_name = 0, None
    if target_id is not None:
        row = conn.execute(
            "SELECT u.name, m.role FROM users u LEFT JOIN memberships m ON m.user_id = u.id AND m.team_id = ? "
            "WHERE u.id = ?", (item["team_id"], target_id)).fetchone()
        if row is None:
            raise Unprocessable("Unknown user", code="unknown_user")
        target_rank, target_name = policy.ROLE_RANK.get(row["role"], 0), row["name"]
    policy.check_assign(user, item, target_id, target_rank)
    if target_id == item["assignee_id"]:
        return queries.item_detail(conn, user, item_id)
    changes: dict = {"assignee_id": target_id}
    if target_id is None and item["status"] in ("in_progress", "blocked"):
        changes["status"] = "open"  # nobody is working on it any more
    new = _guarded_update(conn, item, changes)
    events.record(conn, new, user.id, "assigned",
                  {"from": item["assignee_id"], "to": target_id, "to_name": target_name,
                   "via": "unassign" if target_id is None else "assign",
                   "status": [item["status"], new["status"]]}, versioned=True)
    events.add_watcher(conn, item_id, target_id)
    return queries.item_detail(conn, user, item_id)


def move(conn: sqlite3.Connection, user: UserCtx, item_id: int, team_id: int,
         expected_version: int | None) -> dict:
    """Hand an item to another team's queue. Ownership resets: the new team triages it."""
    item = _load_visible(conn, user, item_id)
    _check_expected_version(conn, user, item, expected_version)
    policy.check_move(user, item)
    target = _team_exists(conn, team_id)
    if target["id"] == item["team_id"]:
        raise Unprocessable("Item is already in that team", code="same_team")
    source = _team_exists(conn, item["team_id"])
    new = _guarded_update(conn, item, {"team_id": target["id"], "assignee_id": None, "status": "open"})
    events.record(conn, new, user.id, "moved",
                  {"from_team": source, "to_team": target, "previous_assignee": item["assignee_id"],
                   "status": [item["status"], "open"]}, versioned=True)
    # The mover may lose visibility of the item; return what they can still see.
    if policy.can_view(user, new):
        return queries.item_detail(conn, user, item_id)
    return {"id": item_id, "moved_to": target, "visible": False}


# --------------------------------------------------------------------------- workflow

def transition(conn: sqlite3.Connection, user: UserCtx, item_id: int, action: str,
               expected_version: int | None, note: str | None) -> dict:
    item = _load_visible(conn, user, item_id)
    _check_expected_version(conn, user, item, expected_version)
    policy.check_transition(user, item, action)
    t = TRANSITIONS[action]
    note = (note or "").strip() or None
    if t.note_required and not note:
        raise Unprocessable(f"A note is required to {action}", code="note_required")

    now = now_iso()
    changes: dict = {"status": t.to_status}
    data: dict = {"action": action, "from": item["status"], "to": t.to_status, "note": note}
    if action == "submit_for_approval":
        changes["approval_requested_by"] = user.id
    elif action == "approve":
        changes.update(approved_by=user.id, approved_at=now)
        data["requested_by"] = item["approval_requested_by"]
    elif action == "reject":
        changes.update(approval_requested_by=None)
        data["requested_by"] = item["approval_requested_by"]
    elif action in ("resolve", "cancel"):
        changes.update(resolution=note, resolved_at=now)
    elif action == "reopen":
        changes.update(resolution=None, resolved_at=None, approval_requested_by=None, approved_by=None,
                       approved_at=None, sla_breached_at=None)
    new = _guarded_update(conn, item, changes)
    events.record(conn, new, user.id, "status_changed", data, versioned=True)
    return queries.item_detail(conn, user, item_id)


# --------------------------------------------------------------------------- collaboration

def add_comment(conn: sqlite3.Connection, user: UserCtx, item_id: int, body: str) -> dict:
    """Comments are history events. They do not bump the item version: a comment never makes
    someone else's pending edit stale."""
    item = _load_visible(conn, user, item_id)
    policy.check_comment(user, item)
    body = body.strip()
    if not body:
        raise Unprocessable("Comment cannot be empty", code="empty_comment")
    conn.execute("UPDATE work_items SET last_activity_at = ? WHERE id = ?", (now_iso(), item_id))
    event_id = events.record(conn, item, user.id, "commented", {"body": body}, versioned=False)
    events.add_watcher(conn, item_id, user.id)
    row = conn.execute(
        "SELECT e.*, u.name AS actor_name FROM item_events e LEFT JOIN users u ON u.id = e.actor_id WHERE e.id = ?",
        (event_id,),
    ).fetchone()
    return event_out(dict(row))


def set_watch(conn: sqlite3.Connection, user: UserCtx, item_id: int, watching: bool) -> dict:
    _load_visible(conn, user, item_id)
    if watching:
        events.add_watcher(conn, item_id, user.id)
    else:
        conn.execute("DELETE FROM item_watchers WHERE item_id = ? AND user_id = ?", (item_id, user.id))
    return {"watching": watching}

