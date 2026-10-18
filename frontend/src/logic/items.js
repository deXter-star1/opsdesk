// Pure functions for reconciling client state with server decisions. No React here, so these
// are unit-tested directly (tests/items.test.mjs).

export const EDITABLE_FIELDS = ["title", "description", "priority", "kind", "due_at", "requires_approval"];

// What a claim will most likely look like, shown immediately while the request is in flight.
export function optimisticClaim(item, me) {
  return {
    ...item,
    assignee: { id: me.id, name: me.name },
    status: item.status === "open" ? "in_progress" : item.status,
    allowed_actions: (item.allowed_actions || []).filter((a) => a !== "claim"),
    _pending: "claim",
  };
}

// After a mutation the server returns the authoritative item. Use it unless what we already
// hold is newer (a realtime refetch may have landed first).
export function reconcile(current, fromServer) {
  if (!fromServer) return current;
  if (current && typeof current.version === "number" && fromServer.version < current.version) return current;
  const { _pending, ...rest } = fromServer;
  return rest;
}

// Keep list pages consistent with a newer copy of one item without refetching the whole list.
export function patchListPages(listData, item) {
  if (!listData || !Array.isArray(listData.pages)) return listData;
  let changed = false;
  const pages = listData.pages.map((page) => {
    const items = page.items.map((row) => {
      if (row.id !== item.id || row.version > item.version) return row;
      changed = true;
      const next = { ...row };
      for (const k of Object.keys(row)) if (k in item) next[k] = item[k];
      return next;
    });
    return { ...page, items };
  });
  return changed ? { ...listData, pages } : listData;
}

// Only the fields the user actually changed, compared to the version they started editing.
export function diffDraft(base, draft) {
  const out = {};
  for (const f of EDITABLE_FIELDS) {
    if (!(f in draft)) continue;
    const a = normalize(f, base[f]);
    const b = normalize(f, draft[f]);
    if (a === b) continue;
    if (f === "due_at") out[f] = b ? new Date(draft[f]).toISOString() : null;
    else if (f === "priority") out[f] = Number(draft[f]);
    else out[f] = draft[f];
  }
  return out;
}

function normalize(field, v) {
  if (field === "due_at") return v ? new Date(v).toISOString().slice(0, 16) : null;
  if (field === "priority") return Number(v);
  if (field === "requires_approval") return Boolean(v);
  return v ?? "";
}

// For the conflict dialog: one row per field the user changed, flagging the ones someone else
// changed in the meantime.
export function conflictRows(details) {
  const yours = details.your_changes || {};
  const theirs = details.item || {};
  const conflicting = new Set(details.conflicting_fields || []);
  return Object.keys(yours).map((field) => ({
    field,
    yours: yours[field],
    theirs: theirs[field],
    conflicting: conflicting.has(field),
  }));
}

// Draft after choosing "use theirs": conflicting fields take the server value, the rest of the
// user's edits are kept, and the new base is the server's current version.
export function acceptTheirs(draft, details) {
  const next = { ...draft };
  for (const f of details.conflicting_fields || []) next[f] = details.item[f];
  return next;
}

// Should a realtime event about this item cause a refetch of what we hold?
export function eventIsNewer(event, cached) {
  if (!cached) return false;
  if (event.item_version == null) return true; // comments, SLA flags: no version, always refresh history
  return event.item_version > cached.version;
}
