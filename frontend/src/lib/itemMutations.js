// Every change to a work item goes through here so that the server's answer is applied the same
// way everywhere: item detail, any cached list pages that contain it, its history, the dashboard.
import { optimisticClaim, patchListPages, reconcile } from "../logic/items.js";
import { api, newIdempotencyKey } from "./api.js";
import { cache } from "./queryCache.js";
import { toast } from "./toasts.js";

export function applyServerItem(item) {
  if (!item || item.visible === false) return;
  cache.setData(["item", item.id], (cur) => reconcile(cur, item));
  cache.updateWhere(["items"], (d) => patchListPages(d, item));
  cache.invalidate(["events", item.id]);
  cache.invalidate(["dashboard"]);
}

// Optimistic: the UI shows "you own this" immediately. If someone else won the race, the server
// says so (409 already_assigned) and includes the item as it really is; we show that instead.
export async function claimItem(item, me) {
  const rollback = cache.optimistic(["item", item.id], (cur) => optimisticClaim(cur, me));
  try {
    const out = await api(`/items/${item.id}/claim`, { method: "POST", idempotencyKey: newIdempotencyKey() });
    applyServerItem(out);
    toast("You own this now", { kind: "success" });
  } catch (e) {
    rollback();
    if (e.code === "already_assigned" && e.details.item) {
      applyServerItem(e.details.item);
      toast(`${e.details.item.assignee?.name || "Someone"} claimed this a moment before you`, { kind: "warn", timeout: 7000 });
      return;
    }
    toast(e.message, { kind: "error" });
    throw e;
  }
}

// Status changes are NOT optimistic: they are decisions (approve, resolve) and the user should
// see the server's verdict. expected_version makes the server refuse if the item changed since
// the user looked at it; the 409 carries the current item and what changed.
export async function transitionItem(item, action, note, key) {
  try {
    const out = await api(`/items/${item.id}/transitions`, {
      method: "POST",
      body: { action, expected_version: item.version, note: note || null },
      idempotencyKey: key || newIdempotencyKey(),
    });
    applyServerItem(out);
    return { ok: true, item: out };
  } catch (e) {
    if (e.code === "stale_version" && e.details.item) {
      applyServerItem(e.details.item);
      return { ok: false, stale: e.details };
    }
    throw e;
  }
}

export async function assignItem(item, assigneeId) {
  try {
    const out = await api(`/items/${item.id}/assign`, {
      method: "POST",
      body: { assignee_id: assigneeId, expected_version: item.version },
      idempotencyKey: newIdempotencyKey(),
    });
    applyServerItem(out);
    return { ok: true };
  } catch (e) {
    if (e.code === "stale_version" && e.details.item) {
      applyServerItem(e.details.item);
      return { ok: false, stale: e.details };
    }
    throw e;
  }
}

export async function moveItem(item, teamId) {
  const out = await api(`/items/${item.id}/move`, {
    method: "POST",
    body: { team_id: teamId, expected_version: item.version },
    idempotencyKey: newIdempotencyKey(),
  });
  applyServerItem(out);
  return out;
}

// Field edits: only changed fields are sent, with the version the user started from. The server
// merges if nobody touched the same fields, or answers 409 edit_conflict with both sides.
export async function saveEdits(itemId, baseVersion, changes, key) {
  const out = await api(`/items/${itemId}`, {
    method: "PATCH",
    body: { expected_version: baseVersion, changes },
    idempotencyKey: key,
  });
  applyServerItem(out);
  return out;
}

export async function setWatching(item, watching) {
  cache.optimistic(["item", item.id], (cur) => ({
    ...cur, watching, watcher_count: cur.watcher_count + (watching ? 1 : -1),
  }));
  try {
    await api(`/items/${item.id}/watch`, { method: watching ? "PUT" : "DELETE" });
  } catch (e) {
    cache.invalidate(["item", item.id]);
    toast(e.message, { kind: "error" });
  }
}
