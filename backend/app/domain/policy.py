"""Authorization and workflow rules — the single source of truth.

Every service command calls one of the `check_*` functions on the *current* row before
writing. The API also returns `allowed_actions(user, item)` with each item so the UI can
hide buttons, but that list is computed from the very same checks: hiding a button is a
convenience, never the enforcement.

Model
  * Roles are per team: viewer < member < lead.
  * Visibility is resource-level: you can see an item if you belong to its team (any role)
    OR you reported it. Reporters who are not in the team (e.g. Support filing a request
    for Payments) can follow and comment on their request but not change it.
  * Items you cannot see return 404, not 403, so their existence does not leak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from .workflow import ACTIVE, ASSIGNABLE, TRANSITIONS

ROLE_RANK = {"viewer": 1, "member": 2, "lead": 3}
VIEWER, MEMBER, LEAD = 1, 2, 3


@dataclass(frozen=True)
class UserCtx:
    id: int
    email: str
    name: str
    is_admin: bool
    roles: dict = field(default_factory=dict)  # team_id -> role name

    def rank(self, team_id: int) -> int:
        return ROLE_RANK.get(self.roles.get(team_id), 0)

    @property
    def team_ids(self) -> list[int]:
        return sorted(self.roles)

    def lead_team_ids(self) -> list[int]:
        return sorted(t for t, r in self.roles.items() if r == "lead")


# --------------------------------------------------------------------------- visibility

def can_view(user: UserCtx, item: dict) -> bool:
    return user.rank(item["team_id"]) >= VIEWER or item["reporter_id"] == user.id


def require_view(user: UserCtx, item: dict | None) -> dict:
    if item is None or not can_view(user, item):
        raise NotFound("Work item not found")
    return item


# --------------------------------------------------------------------------- helpers

def _is_assignee(user: UserCtx, item: dict) -> bool:
    return item["assignee_id"] == user.id and user.rank(item["team_id"]) >= MEMBER


def _actor_allowed(kind: str, user: UserCtx, item: dict) -> bool:
    rank = user.rank(item["team_id"])
    is_reporter = item["reporter_id"] == user.id
    if kind == "lead":
        return rank >= LEAD
    if kind == "assignee_or_lead":
        return rank >= LEAD or _is_assignee(user, item)
    if kind == "lead_or_reporter":
        return rank >= LEAD or is_reporter
    if kind == "member_or_reporter":
        return rank >= MEMBER or is_reporter
    raise ValueError(f"unknown actor kind {kind}")


def _require_active(item: dict) -> None:
    if item["status"] not in ACTIVE:
        raise Conflict(f"Item is {item['status']}; reopen it first", code="item_closed",
                       details={"status": item["status"]})


# --------------------------------------------------------------------------- checks

def check_transition(user: UserCtx, item: dict, action: str) -> None:
    t = TRANSITIONS.get(action)
    if t is None:
        raise Unprocessable(f"Unknown action '{action}'", code="unknown_action")
    if not _actor_allowed(t.actor, user, item):
        raise Forbidden(f"Your role does not allow '{action}' on this item")
    if item["status"] not in t.from_statuses:
        raise Conflict(
            f"Cannot {action} an item that is {item['status']}",
            code="invalid_transition",
            details={"status": item["status"], "action": action},
        )
    if action == "start" and item["assignee_id"] is None:
        raise Conflict("Assign or claim the item before starting work", code="needs_assignee")
    if action == "submit_for_approval" and not item["requires_approval"]:
        raise Conflict("This item does not require approval", code="approval_not_required")
    if action == "resolve" and item["requires_approval"] and item["status"] != "approved":
        raise Conflict("This item requires approval before it can be resolved", code="approval_required")
    if action in ("approve", "reject"):
        # Separation of duties: nobody signs off on their own work.
        if user.id in (item["assignee_id"], item["approval_requested_by"]):
            raise Forbidden("You cannot approve or reject work you requested or are assigned to",
                            code="separation_of_duties")


def check_edit(user: UserCtx, item: dict, fields: set[str]) -> None:
    if user.rank(item["team_id"]) < MEMBER:
        raise Forbidden("Only team members can edit this item")
    _require_active(item)
    if "requires_approval" in fields:
        if user.rank(item["team_id"]) < LEAD:
            raise Forbidden("Only a team lead can change whether approval is required",
                            code="lead_required")
        if item["status"] not in ASSIGNABLE:
            raise Conflict("Approval requirement cannot change while approval is pending or granted",
                           code="approval_in_progress")


def check_claim(user: UserCtx, item: dict) -> None:
    if user.rank(item["team_id"]) < MEMBER:
        raise Forbidden("Only team members can take responsibility for items")
    _require_active(item)
    if item["assignee_id"] is not None and item["assignee_id"] != user.id:
        raise Conflict("Someone else already owns this item", code="already_assigned",
                       details={"assignee_id": item["assignee_id"]})
    if item["assignee_id"] is None and item["status"] != "open":
        raise Conflict(f"Cannot claim an item that is {item['status']}", code="invalid_transition")


def check_assign(user: UserCtx, item: dict, target_id: int | None, target_rank: int) -> None:
    """target_rank is the target user's role rank in the item's team (0 if not a member)."""
    rank = user.rank(item["team_id"])
    if rank < MEMBER:
        raise Forbidden("Only team members can change ownership")
    _require_active(item)
    if item["status"] not in ASSIGNABLE:
        raise Conflict(f"Ownership cannot change while the item is {item['status']}",
                       code="not_assignable", details={"status": item["status"]})
    is_lead = rank >= LEAD
    current = item["assignee_id"]
    if target_id is None:
        if current is not None and not (is_lead or current == user.id):
            raise Forbidden("Only the current owner or a lead can unassign")
        return
    if target_rank < MEMBER:
        raise Unprocessable("Assignee must be a member or lead of the item's team",
                            code="assignee_not_eligible")
    if is_lead:
        return
    if current == user.id:
        return  # the current owner may hand off to another team member
    if current is None and target_id == user.id:
        return  # taking an unowned item yourself
    raise Forbidden("Only a lead can assign work owned by someone else, or assign it to others")


def check_move(user: UserCtx, item: dict) -> None:
    if user.rank(item["team_id"]) < MEMBER:
        raise Forbidden("Only team members can move items to another team")
    _require_active(item)
    if item["status"] not in ASSIGNABLE:
        raise Conflict("Items cannot change team while approval is pending or granted",
                       code="not_movable")


def check_comment(user: UserCtx, item: dict) -> None:
    require_view(user, item)


# --------------------------------------------------------------------------- UI hints

def _passes(fn, *args) -> bool:
    try:
        fn(*args)
        return True
    except (Forbidden, Conflict, Unprocessable):
        return False


def allowed_actions(user: UserCtx, item: dict) -> list[str]:
    actions: list[str] = []
    if item["assignee_id"] is None and _passes(check_claim, user, item):
        actions.append("claim")
    for name in TRANSITIONS:
        if _passes(check_transition, user, item, name):
            actions.append(name)
    # "assign" = may pick an arbitrary eligible team member (leads, or the current owner handing off).
    # Probe with a placeholder target id (-1) that is an eligible member but not the caller.
    if _passes(check_assign, user, item, -1, MEMBER):
        actions.append("assign")
    if item["assignee_id"] is not None and _passes(check_assign, user, item, None, 0):
        actions.append("unassign")
    if _passes(check_edit, user, item, set()):
        actions.append("edit")
    if _passes(check_edit, user, item, {"requires_approval"}):
        actions.append("edit_approval_flag")
    if _passes(check_move, user, item):
        actions.append("move")
    actions.append("comment")  # anyone who can see an item can comment on it
    return actions
