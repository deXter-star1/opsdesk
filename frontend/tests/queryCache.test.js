// Run: npm test   (node --test, no extra dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";
import { QueryCache, keyStartsWith } from "../src/lib/queryCache.js";

test("a slow, older response never overwrites a newer version", async () => {
  const cache = new QueryCache();
  let resolveSlow;
  const slow = new Promise((r) => (resolveSlow = r));
  const pending = cache.fetch(["item", 1], () => slow);               // GET started at v1
  cache.setData(["item", 1], { id: 1, version: 2, status: "in_progress" }); // mutation returned v2
  resolveSlow({ id: 1, version: 1, status: "open" });                  // stale GET lands late
  await pending;
  assert.equal(cache.peek(["item", 1]).data.version, 2);
  assert.equal(cache.peek(["item", 1]).data.status, "in_progress");
});

test("optimistic write can be rolled back to the exact snapshot", () => {
  const cache = new QueryCache();
  const original = { id: 7, version: 3, assignee: null };
  cache.setData(["item", 7], original);
  const rollback = cache.optimistic(["item", 7], (cur) => ({ ...cur, assignee: { id: 1 } }));
  assert.deepEqual(cache.peek(["item", 7]).data.assignee, { id: 1 });
  rollback();
  assert.equal(cache.peek(["item", 7]).data, original);
});

test("invalidate refetches only entries someone is watching", async () => {
  const cache = new QueryCache();
  let calls = { a: 0, b: 0 };
  await cache.fetch(["items", { q: "a" }], async () => (calls.a++, { pages: [] }));
  await cache.fetch(["items", { q: "b" }], async () => (calls.b++, { pages: [] }));
  const unsubscribe = cache.subscribe(["items", { q: "a" }], () => {});
  cache.invalidate(["items"]);
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(calls, { a: 2, b: 1 });
  assert.equal(cache.peek(["items", { q: "b" }]).stale, true); // refetched when next viewed
  unsubscribe();
});

test("concurrent fetches of the same key share one request", async () => {
  const cache = new QueryCache();
  let n = 0;
  const f = async () => (n++, { ok: true });
  await Promise.all([cache.fetch(["me"], f), cache.fetch(["me"], f), cache.fetch(["me"], f)]);
  assert.equal(n, 1);
});

test("key prefixes compare structurally", () => {
  assert.ok(keyStartsWith(["items", { q: "x" }], ["items"]));
  assert.ok(keyStartsWith(["item", 4], ["item", 4]));
  assert.ok(!keyStartsWith(["item", 4], ["item", 40]));
  assert.ok(!keyStartsWith(["events", 4], ["item"]));
});
