"""Simultaneous actions by multiple users.

Risk: two people both believe they own an incident; two leads both approve; a write based on
an old read silently overwrites a newer one.
"""
from __future__ import annotations

import threading

from app import db
from app.errors import Conflict
from app.services import items
from app.services.items import _guarded_update, _load
from support import Client, DBTestCase, LiveServer


def _race(n: int, fn):
    """Start n threads at the same instant; collect (result | exception) from each."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - we want to inspect every outcome
            results[i] = exc
        finally:
            db.close_thread_conn()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


class ClaimRaceTests(DBTestCase):
    def test_concurrent_claims_have_exactly_one_winner(self):
        item = self.create()
        claimers = ["pay_member", "pay_member2", "pay_lead", "pay_lead2"] * 3  # 12 concurrent requests

        def claim(i):
            user = self.ctx(claimers[i])
            with db.write_tx() as conn:
                return items.claim(conn, user, item["id"])

        results = _race(len(claimers), claim)
        winners = [r for r in results if isinstance(r, dict)]
        losers = [r for r in results if isinstance(r, Conflict)]
        winner_id = self.row(item["id"])["assignee_id"]
        # The same user may "win" more than once (claiming your own item is an idempotent no-op),
        # but every success must name the same owner and every failure must be already_assigned.
        self.assertTrue(winners)
        self.assertEqual({w["assignee"]["id"] for w in winners}, {winner_id})
        self.assertEqual(len(winners) + len(losers), len(claimers))
        self.assertTrue(all(e.code == "already_assigned" for e in losers))
        self.assertTrue(all(e.details["item"]["assignee"]["id"] == winner_id for e in losers))
        # Exactly one ownership change was recorded, and the version moved exactly once.
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events WHERE item_id = ? AND kind = 'assigned'",
                                    item["id"]), 1)
        self.assertEqual(self.row(item["id"])["version"], 2)

    def test_concurrent_approvals_apply_once(self):
        item = self.create(kind="payment")
        self.run_cmd(items.claim, self.ctx("pay_member"), item["id"])
        self.run_cmd(items.transition, self.ctx("pay_member"), item["id"], "submit_for_approval", None, None)
        version = self.row(item["id"])["version"]

        def approve(i):
            user = self.ctx(["pay_lead", "pay_lead2"][i])
            with db.write_tx() as conn:
                return items.transition(conn, user, item["id"], "approve", version, None)

        results = _race(2, approve)
        ok = [r for r in results if isinstance(r, dict)]
        failed = [r for r in results if isinstance(r, Conflict)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(failed), 1)
        self.assertIn(failed[0].code, ("stale_version", "invalid_transition"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events WHERE item_id = ? AND data LIKE '%\"approve\"%'",
                                    item["id"]), 1)


class VersionGuardTests(DBTestCase):
    def test_write_based_on_a_stale_read_is_rejected(self):
        """Simulates the interleaving that READ COMMITTED databases allow (read, someone else
        commits, then write). The version guard must refuse instead of overwriting, which is what
        keeps the code correct beyond SQLite's single-writer lock."""
        item = self.create()
        stale = _load(db.get_conn(), item["id"])                      # T1 reads version 1
        self.run_cmd(items.claim, self.ctx("pay_member2"), item["id"])  # T2 commits version 2
        with self.assertRaises(Conflict) as cm:
            with db.write_tx() as conn:                                # T1 writes based on version 1
                _guarded_update(conn, stale, {"assignee_id": self.users["pay_member"]})
        self.assertEqual(cm.exception.code, "concurrent_modification")
        self.assertEqual(self.row(item["id"])["assignee_id"], self.users["pay_member2"])


class HttpClaimRaceTests(DBTestCase):
    def test_concurrent_claims_over_http(self):
        item = self.create()
        names = ["pay_member", "pay_member2", "pay_lead", "pay_lead2"]
        with LiveServer() as srv:
            clients = [Client(srv.port).login(n) for n in names]
            results = _race(len(clients),
                            lambda i: clients[i].post(f"/api/items/{item['id']}/claim")[:2])
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [200, 409, 409, 409])
        winner = next(body for s, body in results if s == 200)
        for s, body in results:
            if s == 409:
                self.assertEqual(body["error"]["code"], "already_assigned")
                self.assertEqual(body["error"]["details"]["item"]["assignee"]["id"], winner["assignee"]["id"])
