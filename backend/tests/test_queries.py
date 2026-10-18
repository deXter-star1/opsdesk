"""Search, filtering and pagination at scale.

Risk: pages that skip or repeat items, searches that break on user input, dashboard tiles that
disagree with the lists they link to.
"""
from __future__ import annotations

from app import db
from app.errors import Unprocessable
from app.services import items, queries
from app.services.queries import SORTS, ItemFilters
from app.timeutil import now_iso
from support import DBTestCase


class PaginationTests(DBTestCase):
    def setUp(self):
        super().setUp()
        # 137 items sharing a handful of identical timestamps and priorities: worst case for ties.
        conn = db.get_conn()
        stamp = now_iso()
        with db.write_tx():
            for i in range(137):
                conn.execute(
                    """INSERT INTO work_items (team_id, title, kind, priority, status, reporter_id, version,
                           due_at, created_at, updated_at, last_activity_at)
                       VALUES (?, ?, 'task', ?, 'open', ?, 1, ?, ?, ?, ?)""",
                    (self.teams["PAY"], f"Item {i}", 1 + i % 4, self.users["pay_member"],
                     None if i % 5 == 0 else f"2026-11-{1 + i % 3:02d}T00:00:00.000000Z", stamp, stamp, stamp))

    def test_keyset_pages_cover_every_item_exactly_once_for_every_sort(self):
        user = self.ctx("pay_member")
        for sort in SORTS:
            seen, cursor, pages = [], None, 0
            while True:
                page = queries.list_items(db.get_conn(), user, ItemFilters(sort=sort, limit=20, cursor=cursor))
                seen += [i["id"] for i in page["items"]]
                pages += 1
                cursor = page["next_cursor"]
                if not cursor:
                    break
            self.assertEqual(len(seen), 137, sort)
            self.assertEqual(len(set(seen)), 137, f"duplicates with sort={sort}")
            self.assertEqual(pages, 7)

    def test_bad_cursor_and_bad_sort_are_rejected(self):
        user = self.ctx("pay_member")
        page = queries.list_items(db.get_conn(), user, ItemFilters(sort="priority", limit=5))
        with self.assertRaises(Unprocessable):  # cursor from another sort
            queries.list_items(db.get_conn(), user, ItemFilters(sort="created", cursor=page["next_cursor"]))
        with self.assertRaises(Unprocessable):
            queries.list_items(db.get_conn(), user, ItemFilters(cursor="not-base64!!"))
        with self.assertRaises(Unprocessable):
            queries.list_items(db.get_conn(), user, ItemFilters(sort="title; DROP TABLE work_items"))


class SearchTests(DBTestCase):
    def test_prefix_search_and_hostile_input(self):
        a = self.create(title="Refund failed for Zenith Foods", description="UPI mandate rejected")
        self.create(title="Laptop purchase approval")
        user = self.ctx("pay_member")

        def search(q):
            return [i["id"] for i in queries.list_items(db.get_conn(), user, ItemFilters(q=q))["items"]]

        self.assertEqual(search("refun zen"), [a["id"]])        # prefix match on every word
        self.assertEqual(search("mandate"), [a["id"]])          # description is indexed
        self.assertEqual(search(f"#{a['id']}"), [a["id"]])      # jump to an id
        for hostile in ('"', "NEAR(", "refund OR *", "-", "'; DROP TABLE work_items; --"):
            search(hostile)                                     # must not raise
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 2)

    def test_search_index_follows_edits(self):
        item = self.create(title="Old wording here")
        self.run_cmd(items.update_item, self.ctx("pay_member"), item["id"], 1, {"title": "Chargeback dispute"})
        user = self.ctx("pay_member")
        self.assertEqual(queries.list_items(db.get_conn(), user, ItemFilters(q="chargeback"))["items"][0]["id"],
                         item["id"])
        self.assertEqual(queries.list_items(db.get_conn(), user, ItemFilters(q="wording"))["items"], [])


class DashboardTests(DBTestCase):
    def test_tiles_match_the_lists_they_link_to(self):
        lead = self.ctx("pay_lead")
        mine = self.create(who="pay_lead", priority=1)
        self.run_cmd(items.claim, lead, mine["id"])
        self.create(priority=2)                                  # unassigned urgent
        pay = self.create(kind="payment")
        self.run_cmd(items.claim, self.ctx("pay_member"), pay["id"])
        self.run_cmd(items.transition, self.ctx("pay_member"), pay["id"], "submit_for_approval", None, None)
        dash = queries.dashboard(db.get_conn(), lead)
        for view, tile in dash["tiles"].items():
            full = queries.list_items(db.get_conn(), lead, ItemFilters(view=view, limit=100))
            self.assertEqual(tile["count"], len(full["items"]), view)
        self.assertEqual(dash["tiles"]["awaiting_my_approval"]["count"], 1)
        self.assertEqual(dash["tiles"]["my_work"]["count"], 1)
        self.assertEqual(dash["tiles"]["unassigned_urgent"]["count"], 1)
        # The member who submitted the payment must not see it as awaiting *their* approval.
        member_dash = queries.dashboard(db.get_conn(), self.ctx("pay_member"))
        self.assertEqual(member_dash["tiles"]["awaiting_my_approval"]["count"], 0)
