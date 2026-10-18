"""Repeated actions: a user (or the client's automatic retry) re-sends a request because they
are unsure whether the first one succeeded.

Risk: duplicate work items, duplicate comments, double transitions.
"""
from __future__ import annotations

import threading

from support import Client, DBTestCase, LiveServer, new_key


def _new_item_body(test, title="Refund failed for order 123"):
    return {"team_id": test.teams["PAY"], "title": title, "kind": "task", "priority": 3}


class IdempotencyTests(DBTestCase):
    def test_retry_with_same_key_replays_and_creates_once(self):
        with LiveServer() as srv:
            c = Client(srv.port).login("pay_member")
            key = new_key()
            s1, b1, h1 = c.post("/api/items", _new_item_body(self), key=key)
            s2, b2, h2 = c.post("/api/items", _new_item_body(self), key=key)
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["id"], b2["id"])
        self.assertNotIn("idempotent-replayed", h1)
        self.assertEqual(h2.get("idempotent-replayed"), "true")
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events WHERE kind = 'created'"), 1)

    def test_concurrent_duplicates_with_same_key_create_once(self):
        key = new_key()
        with LiveServer() as srv:
            clients = [Client(srv.port).login("pay_member") for _ in range(6)]
            barrier = threading.Barrier(len(clients))
            out = []

            def go(c):
                barrier.wait()
                out.append(c.post("/api/items", _new_item_body(self), key=key)[:2])

            threads = [threading.Thread(target=go, args=(c,)) for c in clients]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual({s for s, _ in out}, {201})
        self.assertEqual(len({b["id"] for _, b in out}), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 1)

    def test_same_key_with_different_payload_is_rejected(self):
        with LiveServer() as srv:
            c = Client(srv.port).login("pay_member")
            key = new_key()
            c.post("/api/items", _new_item_body(self, "First title"), key=key)
            status, body, _ = c.post("/api/items", _new_item_body(self, "Different title"), key=key)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "idempotency_key_reused")
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 1)

    def test_failed_request_is_not_cached_so_retry_is_reevaluated(self):
        item = self.create()
        with LiveServer() as srv:
            lead = Client(srv.port).login("pay_lead")
            key = new_key()
            # Starting work fails: nobody owns the item yet.
            s1, b1, _ = lead.post(f"/api/items/{item['id']}/transitions", {"action": "start"}, key=key)
            self.assertEqual((s1, b1["error"]["code"]), (409, "needs_assignee"))
            # The situation changes (the lead assigns it), and the retry with the same key now succeeds
            # instead of replaying the old failure.
            s, _, _ = lead.post(f"/api/items/{item['id']}/assign", {"assignee_id": self.users["pay_member"]})
            self.assertEqual(s, 200)
            s2, b2, h2 = lead.post(f"/api/items/{item['id']}/transitions", {"action": "start"}, key=key)
        self.assertEqual(s2, 200)
        self.assertEqual(b2["status"], "in_progress")
        self.assertNotIn("idempotent-replayed", h2)

    def test_keys_are_scoped_per_user(self):
        key = new_key()
        with LiveServer() as srv:
            a = Client(srv.port).login("pay_member")
            b = Client(srv.port).login("pay_member2")
            s1, b1, _ = a.post("/api/items", _new_item_body(self), key=key)
            s2, b2, _ = b.post("/api/items", _new_item_body(self), key=key)
        self.assertEqual((s1, s2), (201, 201))
        self.assertNotEqual(b1["id"], b2["id"])

    def test_retried_comment_is_posted_once(self):
        item = self.create()
        with LiveServer() as srv:
            c = Client(srv.port).login("pay_member")
            key = new_key()
            for _ in range(3):
                status, _, _ = c.post(f"/api/items/{item['id']}/comments", {"body": "Called the bank"}, key=key)
                self.assertEqual(status, 201)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events WHERE kind = 'commented'"), 1)
