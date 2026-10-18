"""The work-item state machine.

    open ──start──▶ in_progress ──resolve──────────────────────────────▶ resolved
     ▲  (claim also starts)  │  ▲                                          │
     │                block  │  │ unblock / reject                         │
     │                       ▼  │                                          │
     │                    blocked                                          │
     │         in_progress ──submit_for_approval──▶ awaiting_approval      │
     │                                   approve ──▶ approved ──resolve──▶ │
     │                                                                     │
     └──────────────── reopen ◀──── resolved / cancelled ◀── cancel (from any active state)

Rules that are not expressible as "from-state → to-state" live in policy.check_transition:
  * start requires an assignee;
  * submit_for_approval only applies to items that require approval;
  * an item that requires approval can only be resolved from `approved`;
  * approver must not be the assignee or the person who requested approval.
"""
from __future__ import annotations

from dataclasses import dataclass

STATUSES = ("open", "in_progress", "blocked", "awaiting_approval", "approved", "resolved", "cancelled")
TERMINAL = frozenset({"resolved", "cancelled"})
ACTIVE = frozenset(STATUSES) - TERMINAL
# States in which the assignee may change. While an approval is pending or granted the
# assignee is frozen, because separation of duties is evaluated against the assignee.
ASSIGNABLE = frozenset({"open", "in_progress", "blocked"})
KINDS = ("incident", "customer_issue", "engineering", "payment", "compliance", "task")
# Kinds that need sign-off by default (the creator can still change it if they are a lead).
APPROVAL_BY_DEFAULT = frozenset({"payment", "compliance"})


@dataclass(frozen=True)
class Transition:
    action: str
    from_statuses: frozenset
    to_status: str
    actor: str  # who may perform it, see policy._actor_allowed
    note_required: bool = False
    label: str = ""


TRANSITIONS: dict[str, Transition] = {
    t.action: t
    for t in (
        Transition("start", frozenset({"open"}), "in_progress", "assignee_or_lead", label="Start work"),
        Transition("block", frozenset({"in_progress"}), "blocked", "assignee_or_lead", True, "Mark blocked"),
        Transition("unblock", frozenset({"blocked"}), "in_progress", "assignee_or_lead", label="Unblock"),
        Transition("submit_for_approval", frozenset({"in_progress"}), "awaiting_approval", "assignee_or_lead",
                   label="Request approval"),
        Transition("approve", frozenset({"awaiting_approval"}), "approved", "lead", label="Approve"),
        Transition("reject", frozenset({"awaiting_approval"}), "in_progress", "lead", True, "Reject"),
        Transition("resolve", frozenset({"in_progress", "approved"}), "resolved", "assignee_or_lead", True,
                   "Resolve"),
        Transition("cancel", ACTIVE, "cancelled", "lead_or_reporter", True, "Cancel"),
        Transition("reopen", TERMINAL, "open", "member_or_reporter", True, "Reopen"),
    )
}
