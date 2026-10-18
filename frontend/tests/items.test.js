import assert from "node:assert/strict";
import { test } from "node:test";
import {
  acceptTheirs, conflictRows, diffDraft, eventIsNewer, optimisticClaim, patchListPages, reconcile,
} from "../src/logic/items.js";

const item = {
  id: 5, version: 3, title: "Refund", description: "d", priority: 3, kind: "payment", status: "open",
  due_at: "2026-10-20T10:00:00.000000Z", requires_approval: true, assignee: null,
  allowed_actions: ["claim", "edit", "comment"],
};
const me = { id: 9, name: "Arjun" };

test("optimistic claim shows ownership and hides the claim button", () => {
  const o = optimisticClaim(item, me);
  assert.deepEqual(o.assignee, { id: 9, name: "Arjun" });
  assert.equal(o.status, "in_progress");
  assert.ok(!o.allowed_actions.includes("claim"));
  assert.equal(item.assignee, null); // input untouched
});

test("server answer replaces the optimistic guess, but never with an older version", () => {
  const optimistic = optimisticClaim(item, me);
  const server = { ...item, version: 4, status: "in_progress", assignee: { id: 2, name: "Kabir" } };
  const r = reconcile(optimistic, server);
  assert.equal(r.assignee.name, "Kabir");
  assert.equal(r._pending, undefined);
  const newer = { ...server, version: 6 };
  assert.equal(reconcile(newer, server), newer);
});

test("list pages get the newer row without a refetch", () => {
  const list = { pages: [{ items: [{ id: 1, version: 1, title: "x" }, { id: 5, version: 3, status: "open", title: "Refund" }] }] };
  const patched = patchListPages(list, { ...item, version: 4, status: "in_progress" });
  assert.equal(patched.pages[0].items[1].status, "in_progress");
  assert.equal(patched.pages[0].items[0], list.pages[0].items[0]);
  assert.equal(patchListPages(list, { ...item, version: 2 }), list); // older: ignored
});

test("draft diff sends only changed fields, comparing dates at minute precision", () => {
  const local = new Date(item.due_at);
  const pad = (n) => String(n).padStart(2, "0");
  const sameDue = `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())}T${pad(local.getHours())}:${pad(local.getMinutes())}`;
  assert.deepEqual(diffDraft(item, { title: "Refund", priority: "3", due_at: sameDue, requires_approval: true }), {});
  assert.deepEqual(diffDraft(item, { title: "Refund 2", priority: "1" }), { title: "Refund 2", priority: 1 });
  assert.deepEqual(diffDraft(item, { due_at: "" }), { due_at: null });
});

test("conflict rows flag clashing fields and 'use theirs' keeps the rest of my edits", () => {
  const details = {
    conflicting_fields: ["title"],
    your_changes: { title: "Mine", priority: 1 },
    item: { ...item, version: 4, title: "Theirs" },
  };
  const rows = conflictRows(details);
  assert.deepEqual(rows.map((r) => [r.field, r.conflicting]), [["title", true], ["priority", false]]);
  const draft = acceptTheirs({ title: "Mine", priority: 1, description: "d" }, details);
  assert.deepEqual(draft, { title: "Theirs", priority: 1, description: "d" });
});

test("realtime event triggers refetch only when it is newer than what we hold", () => {
  assert.equal(eventIsNewer({ item_version: 3 }, item), false);
  assert.equal(eventIsNewer({ item_version: 4 }, item), true);
  assert.equal(eventIsNewer({ item_version: null }, item), true); // comment
  assert.equal(eventIsNewer({ item_version: 9 }, undefined), false); // not on screen
});
