"""Asynchronous processing: notifications and SLA checks run in a worker, after the primary action.

Risk: a notification is lost (change committed, job not), invented (job committed, change rolled
back), sent twice (job re-executed), or a crashed/slow worker corrupts a newer attempt.
"""
from __future__ import annotations

import os
from unittest import mock

from app import config, db
from app.jobs import queue
from app.jobs.handlers import HANDLERS
from app.services import items
from app.timeutil import iso_ago
from app.worker import process, run_once, schedule_periodic
from support import DBTestCase


def _drain():
    while run_once("test-worker"):
        pass


def _notifications(test, user):
    return test.count("SELECT COUNT(*) FROM notifications WHERE user_id = ?", test.users[user])


class OutboxTests(DBTestCase):
    def test_job_is_committed_with_the_change_and_vanishes_with_a_rollback(self):
        item = self.create(priority=1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs WHERE kind = 'fanout_notifications'"), 1)
        with self.assertRaises(RuntimeError):
            with db.write_tx() as conn:
                items.claim(conn, self.ctx("pay_member"), item["id"])
                raise RuntimeError("request crashed after the write")
        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs"), 1)          # no orphan job
        self.assertIsNone(self.row(item["id"])["assignee_id"])                # no partial change

    def test_assignment_notifies_the_assignee_once_even_if_fanout_runs_twice(self):
        item = self.create(who="pay_lead")
        self.run_cmd(items.assign, self.ctx("pay_lead"), item["id"], self.users["pay_member"], None)
        _drain()
        self.assertEqual(_notifications(self, "pay_member"), 1)
        # Re-run every fan-out job (simulating at-least-once redelivery).
        with db.write_tx() as conn:
            for row in conn.execute("SELECT payload FROM jobs WHERE kind = 'fanout_notifications'").fetchall():
                HANDLERS["fanout_notifications"](conn, db.loads(row["payload"]))
        self.assertEqual(_notifications(self, "pay_member"), 1)

    def test_actor_is_not_notified_about_their_own_action(self):
        item = self.create(who="pay_member", priority=1)  # P1 -> leads get notified
        _drain()
        self.assertEqual(_notifications(self, "pay_member"), 0)
        self.assertEqual(_notifications(self, "pay_lead"), 1)
        self.assertEqual(_notifications(self, "pay_lead2"), 1)
        self.assertEqual(_notifications(self, "sup_lead"), 0)  # other team's lead: not their business
        del item


class RetryTests(DBTestCase):
    def test_failing_job_is_retried_with_backoff_then_dead_lettered(self):
        self.create(priority=1)
        with db.write_tx() as conn:
            conn.execute("UPDATE jobs SET max_attempts = 3")
        boom = mock.patch.dict(HANDLERS, {"fanout_notifications": mock.Mock(side_effect=RuntimeError("smtp down"))})
        with boom:
            for attempt in range(1, 4):
                with db.write_tx() as conn:  # make it due now, as if the backoff elapsed
                    conn.execute("UPDATE jobs SET run_at = ? WHERE status = 'pending'", (iso_ago(seconds=1),))
                self.assertEqual(run_once("w1"), 1)
                job = dict(db.get_conn().execute("SELECT * FROM jobs").fetchone())
                self.assertEqual(job["attempts"], attempt)
                self.assertIn("smtp down", job["last_error"])
                if attempt < 3:
                    self.assertEqual(job["status"], "pending")
                    self.assertGreater(job["run_at"], job["updated_at"])  # scheduled in the future
        self.assertEqual(job["status"], "dead")
        self.assertEqual(run_once("w1"), 0)  # dead jobs are not picked up again
        # An operator fixes the cause and retries it from the admin page.
        with db.write_tx() as conn:
            self.assertTrue(queue.retry_dead(conn, job["id"]))
        _drain()
        self.assertEqual(db.get_conn().execute("SELECT status FROM jobs").fetchone()[0], "done")
        self.assertEqual(_notifications(self, "pay_lead"), 1)

    def test_handler_writes_are_rolled_back_when_it_fails(self):
        self.create(priority=1)
        original = HANDLERS["fanout_notifications"]

        def half_done(conn, payload):
            original(conn, payload)          # notifications inserted...
            raise RuntimeError("crash after partial work")

        with mock.patch.dict(HANDLERS, {"fanout_notifications": half_done}):
            run_once("w1")
        self.assertEqual(self.count("SELECT COUNT(*) FROM notifications"), 0)  # ...and rolled back

    def test_injected_failures_eventually_succeed(self):
        self.create(priority=1)
        os.environ["OPSDESK_JOB_FAILURE_RATE"] = "0.5"
        config.reload_settings()
        try:
            for _ in range(60):
                with db.write_tx() as conn:
                    conn.execute("UPDATE jobs SET run_at = ? WHERE status = 'pending'", (iso_ago(seconds=1),))
                run_once("w1")
                if db.get_conn().execute("SELECT status FROM jobs").fetchone()[0] in ("done", "dead"):
                    break
        finally:
            os.environ.pop("OPSDESK_JOB_FAILURE_RATE")
            config.reload_settings()
        status = db.get_conn().execute("SELECT status FROM jobs").fetchone()[0]
        self.assertIn(status, ("done", "dead"))
        if status == "done":
            self.assertEqual(_notifications(self, "pay_lead"), 1)


class LeaseTests(DBTestCase):
    def test_zombie_worker_cannot_complete_a_job_it_lost(self):
        self.create(priority=1)
        with db.write_tx() as conn:
            [job_a] = queue.claim(conn, "worker-A", limit=1, lease_seconds=30)
        # Worker A stalls; its lease expires; worker B takes over and finishes.
        with db.write_tx() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at = ?", (iso_ago(seconds=1),))
        self.assertEqual(run_once("worker-B"), 1)
        self.assertEqual(_notifications(self, "pay_lead"), 1)
        # A wakes up and tries to finish its (now stale) attempt.
        self.assertEqual(process(job_a, "worker-A"), "lease_lost")
        job = dict(db.get_conn().execute("SELECT * FROM jobs").fetchone())
        self.assertEqual((job["status"], job["attempts"]), ("done", 2))
        self.assertEqual(_notifications(self, "pay_lead"), 1)  # A's writes were rolled back

    def test_job_that_keeps_crashing_its_worker_is_dead_lettered(self):
        self.create(priority=1)
        with db.write_tx() as conn:
            conn.execute("UPDATE jobs SET max_attempts = 2")
        for _ in range(2):  # claim, then "crash" (lease expires without complete/fail)
            with db.write_tx() as conn:
                queue.claim(conn, "crashy", limit=1, lease_seconds=30)
                conn.execute("UPDATE jobs SET lease_expires_at = ?", (iso_ago(seconds=1),))
        with db.write_tx() as conn:
            self.assertEqual(queue.claim(conn, "next", limit=1, lease_seconds=30), [])
        self.assertEqual(db.get_conn().execute("SELECT status FROM jobs").fetchone()[0], "dead")


class PeriodicJobTests(DBTestCase):
    def test_many_schedulers_enqueue_one_sweep_per_window(self):
        for _ in range(5):  # e.g. five workers each running their scheduler
            schedule_periodic()
        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs WHERE kind = 'sla_sweep'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs WHERE kind = 'maintenance'"), 1)

    def test_sla_breach_is_flagged_and_notified_exactly_once(self):
        item = self.create(who="pay_lead")
        self.run_cmd(items.assign, self.ctx("pay_lead"), item["id"], self.users["pay_member"], None)
        _drain()
        with db.write_tx() as conn:
            conn.execute("UPDATE work_items SET due_at = ? WHERE id = ?", (iso_ago(minutes=5), item["id"]))
            conn.execute("DELETE FROM notifications")
        for _ in range(3):  # three sweeps (e.g. overlapping windows or a re-run)
            with db.write_tx() as conn:
                HANDLERS["sla_sweep"](conn, {})
        _drain()
        self.assertEqual(self.count("SELECT COUNT(*) FROM item_events WHERE kind = 'sla_breached'"), 1)
        self.assertEqual(_notifications(self, "pay_member"), 1)   # owner
        self.assertEqual(_notifications(self, "pay_lead2"), 1)    # leads
        self.assertIsNotNone(self.row(item["id"])["sla_breached_at"])
        self.assertEqual(self.row(item["id"])["version"], 2)      # system flag does not make edits stale
