// Server-state cache.
//
// Everything the UI shows that came from the server lives here, keyed by an array such as
// ["item", 42] or ["items", {filters}]. Components subscribe to keys (see hooks.js) and re-render
// when that entry changes. Three rules make it safe under concurrency:
//
//  1. Monotonic versions. If both the cached and the incoming value carry a `version` (work items
//     do), an incoming value with a LOWER version is discarded. A slow GET that started before a
//     successful mutation can therefore never overwrite the newer state the mutation returned.
//  2. Optimistic writes are snapshots + rollback. `optimistic(key, fn)` applies fn now and returns
//     a rollback function; the caller then replaces it with the server's answer (success) or with
//     the server's current state / the snapshot (failure). The server always has the last word.
//  3. Invalidation, not patching, for things we did not cause. A realtime event marks entries
//     stale; entries someone is looking at are refetched through the normal authorized API.

const BLANK = Object.freeze({ data: undefined, error: null, status: "idle", stale: true, updatedAt: 0 });

export function keyHash(key) {
  return JSON.stringify(key);
}

export function keyStartsWith(key, prefix) {
  if (prefix.length > key.length) return false;
  return prefix.every((part, i) => keyHash(part) === keyHash(key[i]));
}

export function isOlder(next, prev) {
  return (
    prev != null && next != null &&
    typeof prev.version === "number" && typeof next.version === "number" &&
    next.version < prev.version
  );
}

export class QueryCache {
  constructor() {
    this.entries = new Map();  // hash -> entry (immutable snapshots, so React can compare by reference)
    this.listeners = new Map(); // hash -> Set<fn>
    this.fetchers = new Map(); // hash -> () => Promise
    this.inflight = new Map(); // hash -> Promise
  }

  peek(key) {
    return this.entries.get(keyHash(key)) || BLANK;
  }

  _write(key, patch) {
    const h = keyHash(key);
    const prev = this.entries.get(h) || { ...BLANK, key };
    const next = { ...prev, ...patch, key };
    this.entries.set(h, next);
    const subs = this.listeners.get(h);
    if (subs) subs.forEach((fn) => fn());
    return next;
  }

  subscribe(key, fn) {
    const h = keyHash(key);
    if (!this.listeners.has(h)) this.listeners.set(h, new Set());
    this.listeners.get(h).add(fn);
    return () => {
      const set = this.listeners.get(h);
      if (set) {
        set.delete(fn);
        if (set.size === 0) this.listeners.delete(h);
      }
    };
  }

  register(key, fetcher) {
    this.fetchers.set(keyHash(key), fetcher);
  }

  // Fetch (deduplicated: concurrent callers share one request).
  fetch(key, fetcher, { force = false } = {}) {
    const h = keyHash(key);
    if (fetcher) this.fetchers.set(h, fetcher);
    fetcher = fetcher || this.fetchers.get(h);
    if (!fetcher) return Promise.resolve(this.peek(key).data);
    if (this.inflight.has(h) && !force) return this.inflight.get(h);
    const cur = this.peek(key);
    this._write(key, { status: cur.data === undefined ? "loading" : "refreshing" });
    const p = Promise.resolve()
      .then(fetcher)
      .then(
        (data) => {
          if (this.inflight.get(h) === p) this.inflight.delete(h);
          this.setData(key, data);
          return data;
        },
        (error) => {
          if (this.inflight.get(h) === p) this.inflight.delete(h);
          const e = this.peek(key);
          this._write(key, { error, status: e.data === undefined ? "error" : "success" });
          throw error;
        },
      );
    this.inflight.set(h, p);
    return p;
  }

  // Authoritative write (server response). Respects rule 1 (monotonic versions).
  setData(key, valueOrUpdater) {
    const cur = this.peek(key);
    const next = typeof valueOrUpdater === "function" ? valueOrUpdater(cur.data) : valueOrUpdater;
    if (isOlder(next, cur.data)) {
      this._write(key, { stale: false, status: "success" });
      return cur.data;
    }
    this._write(key, { data: next, error: null, status: "success", stale: false, updatedAt: Date.now() });
    return next;
  }

  // Rule 2: apply a speculative change now; returns rollback().
  optimistic(key, updater) {
    const snapshot = this.peek(key).data;
    this._write(key, { data: updater(snapshot) });
    return () => this._write(key, { data: snapshot });
  }

  // Rule 3: mark matching entries stale and refetch the ones currently on screen.
  invalidate(prefix = []) {
    for (const [h, entry] of this.entries) {
      if (!keyStartsWith(entry.key, prefix)) continue;
      this._write(entry.key, { stale: true });
      if (this.listeners.has(h) && this.fetchers.has(h)) {
        this.fetch(entry.key, null, { force: true }).catch(() => {});
      }
    }
  }

  // Visit every cached entry under a prefix (e.g. to update an item inside cached list pages).
  updateWhere(prefix, updater) {
    for (const entry of [...this.entries.values()]) {
      if (!keyStartsWith(entry.key, prefix) || entry.data === undefined) continue;
      const next = updater(entry.data, entry.key);
      if (next !== entry.data) this._write(entry.key, { data: next });
    }
  }

  clear() {
    this.entries.clear();
    this.inflight.clear();
    for (const [, subs] of this.listeners) subs.forEach((fn) => fn());
  }
}

export const cache = new QueryCache();
