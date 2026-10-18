// Fetch wrapper: JSON in/out, uniform errors, idempotency keys and safe retries.
//
// Retry policy
//   GET                         -> retried once on network failure / 502-504 (always safe)
//   mutation WITH a key         -> retried up to twice with the SAME key; the server replays the
//                                  original result if the first attempt actually succeeded
//   mutation WITHOUT a key      -> never retried automatically
//
// "Flaky network" mode (toggle in the sidebar) makes the first response of every mutation get
// "lost" after the server has processed it, which is exactly the situation where a user does not
// know whether their click worked. The retry + server-side replay then shows up in the UI.

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(err.message || `Request failed (${status})`);
    this.status = status;
    this.code = err.code || (status === 0 ? "network_error" : "http_error");
    this.details = err.details || {};
  }
}

const RETRYABLE = new Set([502, 503, 504]);
const listeners = { unauthorized: new Set(), replayed: new Set() };

export function on(event, fn) {
  listeners[event].add(fn);
  return () => listeners[event].delete(fn);
}

let flaky = false;
export function setFlakyNetwork(value) {
  flaky = value;
}
export function isFlakyNetwork() {
  return flaky;
}

export function newIdempotencyKey() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  return `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function api(path, { method = "GET", body, idempotencyKey, retries } = {}) {
  const isMutation = method !== "GET";
  const maxRetries = retries ?? (!isMutation ? 1 : idempotencyKey ? 2 : 0);
  const headers = { Accept: "application/json", "X-Requested-With": "opsdesk" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await fetch(`/api${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
      });
      if (flaky && isMutation && idempotencyKey && attempt === 0) {
        await res.text(); // the server did the work; we just never "receive" the answer
        throw new TypeError("simulated lost response");
      }
    } catch (e) {
      if (attempt < maxRetries) {
        await sleep(300 * 2 ** attempt);
        continue;
      }
      throw new ApiError(0, { error: { code: "network_error", message: "Network error. Your change may not have been saved; retrying is safe." } });
    }
    if (RETRYABLE.has(res.status) && attempt < maxRetries) {
      await sleep(300 * 2 ** attempt);
      continue;
    }
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      const err = new ApiError(res.status, data);
      if (res.status === 401) listeners.unauthorized.forEach((fn) => fn(err));
      throw err;
    }
    if (res.headers.get("Idempotent-Replayed") === "true") {
      listeners.replayed.forEach((fn) => fn({ path, attempt }));
    }
    return data;
  }
}

export function qs(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    sp.set(k, Array.isArray(v) ? v.join(",") : String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}
