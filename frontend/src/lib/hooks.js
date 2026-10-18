import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { cache, keyHash } from "./queryCache.js";

// Subscribe a component to one cache entry and make sure it is loaded.
export function useQuery(key, fetcher, { enabled = true, refetchIntervalMs = 0 } = {}) {
  const h = keyHash(key);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const stableFetcher = useCallback(() => fetcherRef.current(), [h]);

  const subscribe = useCallback((fn) => cache.subscribe(key, fn), [h]);
  const getSnapshot = useCallback(() => cache.peek(key), [h]);
  const entry = useSyncExternalStore(subscribe, getSnapshot);

  useEffect(() => {
    if (!enabled) return undefined;
    cache.register(key, stableFetcher);
    const cur = cache.peek(key);
    if (cur.stale || cur.status === "idle") cache.fetch(key, stableFetcher).catch(() => {});
    if (!refetchIntervalMs) return undefined;
    const t = setInterval(() => cache.fetch(key, stableFetcher).catch(() => {}), refetchIntervalMs);
    return () => clearInterval(t);
  }, [h, enabled, refetchIntervalMs]);

  return {
    data: entry.data,
    error: entry.error,
    loading: entry.status === "loading",
    refreshing: entry.status === "refreshing",
    refetch: () => cache.fetch(key, stableFetcher, { force: true }),
  };
}

// Track a single async action's pending/error state; prevents double submission.
export function useAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const busy = useRef(false);
  const run = useCallback(async (fn) => {
    if (busy.current) return undefined; // second click while the first is in flight: ignored
    busy.current = true;
    setPending(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e);
      throw e;
    } finally {
      busy.current = false;
      setPending(false);
    }
  }, []);
  return { pending, error, run, setError };
}

// An idempotency key that stays the same while the payload is the same (so a retry of the same
// intent reuses it) and changes when the user changes what they are submitting.
export function useIntentKey(payload) {
  const ref = useRef({ hash: null, key: null });
  const hash = JSON.stringify(payload);
  if (ref.current.hash !== hash) ref.current = { hash, key: null };
  return {
    get: () => {
      if (!ref.current.key) ref.current.key = globalThis.crypto?.randomUUID?.() || String(Math.random());
      return ref.current.key;
    },
    reset: () => {
      ref.current = { hash: null, key: null };
    },
  };
}

export function useDebounced(value, ms) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}
