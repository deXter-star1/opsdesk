"""Stale information: users acting on a version of an item that is no longer current.

Risk: a user edits an old copy and silently erases someone else's newer change ("lost update"),
or approves/resolves something that changed under them.
"""
from __future__ import annotations

from app.errors import Conflict
from app.services import items
from support import DBTestCase


class StaleEditTests(DBTestCase):
    def test_non_overlapping_concurrent_edits_merge(self):
        item = self.create(title="Original title", priority=3)
        alice, bob = self.ctx("pay_member"), self.ctx("pay_member2")
        # Both opened the item at version 1.
        self.run_cmd(items.update_item, alice, item["id"], 1, {"title": "Alice's better title"})
        merged = self.run_cmd(items.update_item, bob, item["id"], 1, {"priority": 1})
        self.assertTrue(merged["merged"])
        row = self.row(item["id"])
        self.assertEqual(row["title"], "Alice's better title")  # Alice's change survived
        self.assertEqual(row["priority"], 1)                     # Bob's change applied
        self.assertEqual(row["version"], 3)

    def test_overlapping_concurrent_edits_conflict_and_write_nothing(self):
        item = self.create(title="Original title")
        alice, bob = self.ctx("pay_member"), self.ctx("pay_member2")
        self.run_cmd(items.update_item, alice, item["id"], 1, {"title": "Alice's title"})
        err = self.assertAppError(Conflict, "edit_conflict", self.run_cmd, items.update_item, bob, item["id"], 1,
                                  {"title": "Bob's title", "priority": 2})
        self.assertEqual(err.details["conflicting_fields"], ["title"])
        self.assertEqual(err.details["item"]["title"], "Alice's title")     # current state for the UI
        self.assertEqual(err.details["your_changes"]["title"], "Bob's title")
        self.assertEqual(len(err.details["changes_since"]), 1)
        row = self.row(item["id"])
        self.assertEqual((row["title"], row["priority"], row["version"]), ("Alice's title", 3, 2))

    def test_comments_do_not_make_edits_stale(self):
        item = self.create()
        self.run_cmd(items.add_comment, self.ctx("pay_member2"), item["id"], "FYI customer called again")
        out = self.run_cmd(items.update_item, self.ctx("pay_member"), item["id"], 1, {"priority": 2})
        self.assertFalse(out["merged"])
        self.assertEqual(self.row(item["id"])["version"], 2)

    def test_transition_on_stale_version_is_refused_with_whats_changed(self):
        item = self.create()
        self.run_cmd(items.claim, self.ctx("pay_member"), item["id"])            # v2
        self.run_cmd(items.update_item, self.ctx("pay_member"), item["id"], 2,
                     {"description": "Turns out it is a refund"})              # v3
        # A lead looking at version 2 tries to cancel it.
        err = self.assertAppError(Conflict, "stale_version", self.run_cmd, items.transition, self.ctx("pay_lead"),
                                  item["id"], "cancel", 2, "not needed")
        self.assertEqual(err.details["current_version"], 3)
        self.assertEqual([e["kind"] for e in err.details["changes_since"]], ["updated"])
        self.assertEqual(self.row(item["id"])["status"], "in_progress")

    def test_noop_edit_does_not_bump_version(self):
        item = self.create(priority=3)
        self.run_cmd(items.update_item, self.ctx("pay_member"), item["id"], 1, {"priority": 3})
        self.assertEqual(self.row(item["id"])["version"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events WHERE item_id = ? AND kind = 'updated'",
                                    item["id"]), 0)

    def test_editing_the_deadline_resets_sla_flag(self):
        item = self.create()
        from app import db
        with db.write_tx() as conn:
            conn.execute("UPDATE work_items SET sla_breached_at = due_at WHERE id = ?", (item["id"],))
        self.run_cmd(items.update_item, self.ctx("pay_member"), item["id"], 1, {"due_at": "2099-01-01T00:00:00Z"})
        self.assertIsNone(self.row(item["id"])["sla_breached_at"])
