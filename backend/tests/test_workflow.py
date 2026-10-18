"""Workflow rules and history integrity.

Risk: money moves without sign-off; items reach impossible states; history disagrees with state.
"""
from __future__ import annotations

import itertools

from app import db
from app.domain import policy
from app.domain.workflow import STATUSES, TRANSITIONS
from app.errors import Conflict, Forbidden, Unprocessable
from app.services import items
from support import DBTestCase


class TransitionTableTests(DBTestCase):
    def test_every_status_action_pair_matches_the_table(self):
        """Exhaustive: for each (status, action), the check allows it iff the table says so
        (evaluated as a lead who is not the assignee, with every guard satisfied)."""
        lead = self.ctx("pay_lead")
        for status, action in itertools.product(STATUSES, TRANSITIONS):
            item = {"team_id": self.teams["PAY"], "status": status, "assignee_id": self.users["pay_member"],
                    "reporter_id": self.users["pay_member"], "approval_requested_by": self.users["pay_member"],
                    "requires_approval": 1 if action in ("submit_for_approval",) or status == "approved" else 0}
            allowed = status in TRANSITIONS[action].from_statuses
            try:
                policy.check_transition(lead, item, action)
                passed = True
            except Conflict:
                passed = False
            self.assertEqual(passed, allowed, f"{action} from {status}")


class ApprovalWorkflowTests(DBTestCase):
    def test_full_lifecycle_with_approval(self):
        item = self.create(kind="payment")
        self.assertTrue(item["requires_approval"])  # payment defaults to needing sign-off
        member, lead = self.ctx("pay_member"), self.ctx("pay_lead")
        self.run_cmd(items.claim, member, item["id"])
        self.assertAppError(Conflict, "approval_required", self.run_cmd, items.transition, member, item["id"],
                            "resolve", None, "done")
        self.run_cmd(items.transition, member, item["id"], "submit_for_approval", None, None)
        self.assertAppError(Conflict, "not_assignable", self.run_cmd, items.assign, lead, item["id"],
                            self.users["pay_member2"], None)  # owner frozen while approval pending
        self.run_cmd(items.transition, lead, item["id"], "reject", None, "Attach the bank statement")
        self.assertIsNone(self.row(item["id"])["approval_requested_by"])
        self.run_cmd(items.transition, member, item["id"], "submit_for_approval", None, None)
        self.run_cmd(items.transition, lead, item["id"], "approve", None, None)
        out = self.run_cmd(items.transition, member, item["id"], "resolve", None, "Refund executed, ref #77")
        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["resolution"], "Refund executed, ref #77")

    def test_reopen_clears_approval_so_it_must_be_approved_again(self):
        item = self.create(kind="payment")
        member, lead = self.ctx("pay_member"), self.ctx("pay_lead")
        self.run_cmd(items.claim, member, item["id"])
        self.run_cmd(items.transition, member, item["id"], "submit_for_approval", None, None)
        self.run_cmd(items.transition, lead, item["id"], "approve", None, None)
        self.run_cmd(items.transition, member, item["id"], "resolve", None, "done")
        self.run_cmd(items.transition, member, item["id"], "reopen", None, "Customer says refund not received")
        row = self.row(item["id"])
        self.assertEqual((row["status"], row["approved_by"], row["resolution"]), ("open", None, None))
        self.run_cmd(items.transition, member, item["id"], "start", None, None)
        self.assertAppError(Conflict, "approval_required", self.run_cmd, items.transition, member, item["id"],
                            "resolve", None, "done again")

    def test_notes_are_required_where_a_reason_matters(self):
        item = self.create()
        member = self.ctx("pay_member")
        self.run_cmd(items.claim, member, item["id"])
        for action in ("block", "resolve"):
            self.assertAppError(Unprocessable, "note_required", self.run_cmd, items.transition, member, item["id"],
                                action, None, "   ")

    def test_cannot_start_without_owner_and_unassign_returns_to_open(self):
        item = self.create()
        self.assertAppError(Conflict, "needs_assignee", self.run_cmd, items.transition, self.ctx("pay_lead"),
                            item["id"], "start", None, None)
        self.run_cmd(items.claim, self.ctx("pay_member"), item["id"])
        out = self.run_cmd(items.assign, self.ctx("pay_member"), item["id"], None, None)
        self.assertEqual((out["status"], out["assignee"]), ("open", None))

    def test_closed_items_cannot_be_edited_until_reopened(self):
        item = self.create()
        self.run_cmd(items.transition, self.ctx("pay_lead"), item["id"], "cancel", None, "duplicate")
        self.assertAppError(Conflict, "item_closed", self.run_cmd, items.update_item, self.ctx("pay_member"),
                            item["id"], 2, {"title": "new title"})

    def test_move_to_another_team_resets_ownership(self):
        item = self.create(who="pay_lead")
        self.run_cmd(items.claim, self.ctx("pay_member"), item["id"])
        out = self.run_cmd(items.move, self.ctx("pay_member"), item["id"], self.teams["SUP"], None)
        # pay_member is not in SUP and did not report it: they lose visibility, which the response says.
        self.assertEqual(out, {"id": item["id"], "moved_to": {"id": self.teams["SUP"], "key": "SUP",
                                                              "name": "Support"}, "visible": False})
        row = self.row(item["id"])
        self.assertEqual((row["team_id"], row["assignee_id"], row["status"]), (self.teams["SUP"], None, "open"))


class HistoryIntegrityTests(DBTestCase):
    def test_every_versioned_change_has_exactly_one_matching_event(self):
        item = self.create(kind="payment")
        member, lead = self.ctx("pay_member"), self.ctx("pay_lead")
        steps = [
            (items.claim, member, item["id"]),
            (items.update_item, member, item["id"], 2, {"priority": 1}),
            (items.add_comment, lead, item["id"], "Prioritising this"),
            (items.transition, member, item["id"], "submit_for_approval", None, None),
            (items.transition, lead, item["id"], "approve", None, None),
            (items.transition, member, item["id"], "resolve", None, "done"),
        ]
        for fn, *args in steps:
            self.run_cmd(fn, *args)
        row = self.row(item["id"])
        versions = [r[0] for r in db.get_conn().execute(
            "SELECT item_version FROM item_events WHERE item_id = ? AND item_version IS NOT NULL ORDER BY id",
            (item["id"],))]
        self.assertEqual(versions, list(range(1, row["version"] + 1)))  # 1..N, no gaps, no duplicates
        kinds = [r[0] for r in db.get_conn().execute(
            "SELECT kind FROM item_events WHERE item_id = ? ORDER BY id", (item["id"],))]
        self.assertEqual(kinds, ["created", "assigned", "updated", "commented", "status_changed",
                                 "status_changed", "status_changed"])

    def test_failed_command_leaves_no_trace(self):
        item = self.create()
        before = self.count("SELECT COUNT(*) FROM item_events") , self.count("SELECT COUNT(*) FROM jobs")
        with self.assertRaises(Forbidden):
            self.run_cmd(items.transition, self.ctx("pay_viewer"), item["id"], "cancel", None, "x")
        after = self.count("SELECT COUNT(*) FROM item_events"), self.count("SELECT COUNT(*) FROM jobs")
        self.assertEqual(before, after)
