"""Authorization is enforced by the server, per resource, regardless of what the UI shows.

Risk: data leaking across teams; a viewer (or a crafted request) changing work; someone
approving their own payment.
"""
from __future__ import annotations

import asyncio

from app.errors import Forbidden, Unprocessable
from app.realtime import Broker, visible_to
from app.services import items, queries
from app.services.queries import ItemFilters
from support import Client, DBTestCase, LiveServer


class VisibilityTests(DBTestCase):
    def test_items_of_other_teams_are_invisible_everywhere(self):
        secret = self.create(team="PAY", title="Confidential settlement investigation")
        with LiveServer() as srv:
            outsider = Client(srv.port).login("sup_lead")  # lead of SUP, no PAY role
            self.assertEqual(outsider.get(f"/api/items/{secret['id']}")[0], 404)
            self.assertEqual(outsider.get(f"/api/items/{secret['id']}/events")[0], 404)
            self.assertEqual(outsider.post(f"/api/items/{secret['id']}/claim")[0], 404)
            self.assertEqual(outsider.post(f"/api/items/{secret['id']}/comments", {"body": "hi"})[0], 404)
            _, listing, _ = outsider.get("/api/items?state=all")
            self.assertNotIn(secret["id"], [i["id"] for i in listing["items"]])
            _, search, _ = outsider.get("/api/items?q=confidential&state=all")
            self.assertEqual(search["items"], [])
            _, by_id, _ = outsider.get(f"/api/items?q=%23{secret['id']}")
            self.assertEqual(by_id["items"], [])

    def test_reporter_outside_the_team_can_follow_but_not_change(self):
        item = self.create(who="outsider", team="PAY", title="Please refund my vendor")
        outsider = self.ctx("outsider")
        self.assertEqual(self.run_cmd(queries.item_detail, outsider, item["id"])["id"], item["id"])
        self.run_cmd(items.add_comment, outsider, item["id"], "Any update?")
        self.assertAppError(Forbidden, "forbidden", self.run_cmd, items.update_item, outsider, item["id"], 1,
                            {"title": "Refund NOW"})
        self.assertAppError(Forbidden, "forbidden", self.run_cmd, items.claim, outsider, item["id"])
        listed = self.run_cmd(queries.list_items, outsider, ItemFilters())
        self.assertEqual([i["id"] for i in listed["items"]], [item["id"]])
        # ...but they may withdraw their own request.
        self.run_cmd(items.transition, outsider, item["id"], "cancel", None, "Resolved with vendor directly")
        self.assertEqual(self.row(item["id"])["status"], "cancelled")

    def test_viewer_can_read_and_comment_but_not_act(self):
        item = self.create()
        with LiveServer() as srv:
            viewer = Client(srv.port).login("pay_viewer")
            status, body, _ = viewer.get(f"/api/items/{item['id']}")
            self.assertEqual(status, 200)
            self.assertEqual(body["allowed_actions"], ["comment"])
            # The UI would hide these buttons; the API refuses them anyway.
            self.assertEqual(viewer.patch(f"/api/items/{item['id']}",
                                          {"expected_version": 1, "changes": {"title": "hacked"}})[0], 403)
            self.assertEqual(viewer.post(f"/api/items/{item['id']}/claim")[0], 403)
            self.assertEqual(viewer.post(f"/api/items/{item['id']}/transitions",
                                         {"action": "cancel", "note": "x"})[0], 403)
            self.assertEqual(viewer.post(f"/api/items/{item['id']}/comments", {"body": "Looks urgent"})[0], 201)
        self.assertEqual(self.row(item["id"])["title"], "Investigate duplicate payout")


class OwnershipRuleTests(DBTestCase):
    def test_member_cannot_assign_to_others_but_lead_can(self):
        item = self.create()
        self.assertAppError(Forbidden, "forbidden", self.run_cmd, items.assign, self.ctx("pay_member"),
                            item["id"], self.users["pay_member2"], None)
        out = self.run_cmd(items.assign, self.ctx("pay_lead"), item["id"], self.users["pay_member2"], None)
        self.assertEqual(out["assignee"]["id"], self.users["pay_member2"])

    def test_current_owner_can_hand_off_and_nobody_else_can_take_it(self):
        item = self.create()
        self.run_cmd(items.claim, self.ctx("pay_member"), item["id"])
        self.assertAppError(Forbidden, "forbidden", self.run_cmd, items.assign, self.ctx("pay_member2"),
                            item["id"], self.users["pay_member2"], None)
        out = self.run_cmd(items.assign, self.ctx("pay_member"), item["id"], self.users["pay_member2"], None)
        self.assertEqual(out["assignee"]["id"], self.users["pay_member2"])

    def test_assignee_must_be_member_of_the_items_team(self):
        item = self.create()
        for target in ("pay_viewer", "sup_lead"):
            self.assertAppError(Unprocessable, "assignee_not_eligible", self.run_cmd, items.assign,
                                self.ctx("pay_lead"), item["id"], self.users[target], None)


class ApprovalRuleTests(DBTestCase):
    def _submitted_payment(self, assignee="pay_member"):
        item = self.create(kind="payment")
        self.run_cmd(items.claim, self.ctx(assignee), item["id"])
        self.run_cmd(items.transition, self.ctx(assignee), item["id"], "submit_for_approval", None, None)
        return item

    def test_separation_of_duties(self):
        # A lead who owns the work cannot approve it, even though leads normally can approve.
        item = self._submitted_payment(assignee="pay_lead")
        self.assertAppError(Forbidden, "separation_of_duties", self.run_cmd, items.transition,
                            self.ctx("pay_lead"), item["id"], "approve", None, None)
        self.run_cmd(items.transition, self.ctx("pay_lead2"), item["id"], "approve", None, None)
        self.assertEqual(self.row(item["id"])["approved_by"], self.users["pay_lead2"])

    def test_members_cannot_approve(self):
        item = self._submitted_payment()
        self.assertAppError(Forbidden, "forbidden", self.run_cmd, items.transition, self.ctx("pay_member2"),
                            item["id"], "approve", None, None)

    def test_non_lead_cannot_waive_or_remove_approval(self):
        self.assertAppError(Forbidden, "lead_required", self.create, kind="payment", requires_approval=False)
        item = self.create(kind="payment")
        self.assertAppError(Forbidden, "lead_required", self.run_cmd, items.update_item, self.ctx("pay_member"),
                            item["id"], 1, {"requires_approval": False})
        # A lead may, while no approval is in flight.
        self.run_cmd(items.update_item, self.ctx("pay_lead"), item["id"], 1, {"requires_approval": False})
        self.assertEqual(self.row(item["id"])["requires_approval"], 0)


class ProtocolTests(DBTestCase):
    def test_unauthenticated_csrf_and_admin_guards(self):
        with LiveServer() as srv:
            anon = Client(srv.port)
            self.assertEqual(anon.get("/api/items")[0], 401)
            self.assertEqual(anon.get("/api/stream")[0], 401)
            member = Client(srv.port).login("pay_member")
            status, body, _ = member.post("/api/items", {"team_id": self.teams["PAY"], "title": "abc def",
                                                         "kind": "task", "priority": 3}, csrf=False)
            self.assertEqual((status, body["error"]["code"]), (403, "csrf_check_failed"))
            self.assertEqual(member.get("/api/admin/jobs")[0], 404)
            self.assertEqual(Client(srv.port).login("admin").get("/api/admin/jobs")[0], 200)
            status, _, _ = Client(srv.port).request("POST", "/api/auth/login",
                                                    {"email": "pay_member@test", "password": "wrong"})
            self.assertEqual(status, 401)


class RealtimeVisibilityTests(DBTestCase):
    def test_stream_only_delivers_events_the_subscriber_may_see(self):
        pay_event = {"id": 1, "item_id": 10, "team_id": self.teams["PAY"], "reporter_id": 999, "kind": "updated",
                     "item_version": 2, "actor_id": 1, "actor_name": "x", "data": "{}", "created_at": "t"}
        own_report = {**pay_event, "id": 2, "reporter_id": self.users["outsider"]}

        self.assertTrue(visible_to(self.users["pay_viewer"], frozenset({self.teams["PAY"]}), pay_event))
        self.assertFalse(visible_to(self.users["sup_lead"], frozenset({self.teams["SUP"]}), pay_event))
        self.assertTrue(visible_to(self.users["outsider"], frozenset(), own_report))

        async def scenario():
            b = Broker()
            sup, _ = b.subscribe(self.users["sup_lead"], [self.teams["SUP"]])
            pay, _ = b.subscribe(self.users["pay_viewer"], [self.teams["PAY"]])
            b.dispatch([pay_event], [{"id": 5, "user_id": self.users["pay_viewer"], "item_id": 10, "reason": "x"}])
            return sup.queue.qsize(), pay.queue.qsize()

        self.assertEqual(asyncio.run(scenario()), (0, 2))  # PAY viewer: 1 event + 1 own notification
