// Live updates. The server pushes small "item X changed (now version N)" hints over SSE; we turn
// them into cache invalidations so the affected views refetch through the normal API.
// EventSource reconnects by itself and sends Last-Event-ID, so the server can replay what we
// missed; if we were gone too long it sends `resync` and we refetch everything visible.
import { useSyncExternalStore } from "react";
import { eventIsNewer } from "../logic/items.js";
import { cache } from "./queryCache.js";

let status = "connecting";
const statusListeners = new Set();
const itemListeners = new Set();
let source = null;
let dashboardTimer = null;

function setStatus(s) {
  status = s;
  statusListeners.forEach((fn) => fn());
}

export function useRealtimeStatus() {
  return useSyncExternalStore(
    (fn) => {
      statusListeners.add(fn);
      return () => statusListeners.delete(fn);
    },
    () => status,
  );
}

// Components (list page "new activity" pill, detail page banner) can listen to raw item events.
export function onItemEvent(fn) {
  itemListeners.add(fn);
  return () => itemListeners.delete(fn);
}

function handleItemEvent(ev) {
  const cached = cache.peek(["item", ev.item_id]).data;
  if (eventIsNewer(ev, cached)) cache.invalidate(["item", ev.item_id]);
  cache.invalidate(["events", ev.item_id]);
  // Dashboard counts: coalesce bursts of events into one refetch.
  clearTimeout(dashboardTimer);
  dashboardTimer = setTimeout(() => cache.invalidate(["dashboard"]), 1500);
  itemListeners.forEach((fn) => fn(ev));
}

export function startRealtime() {
  if (source) return stopRealtime;
  source = new EventSource("/api/stream");
  let everConnected = false;
  source.addEventListener("hello", () => {
    if (everConnected) cache.invalidate([]); // reconnected: cheap insurance on top of replay
    everConnected = true;
    setStatus("live");
  });
  source.addEventListener("item", (e) => handleItemEvent(JSON.parse(e.data)));
  source.addEventListener("notification", () => {
    cache.invalidate(["me"]);
    cache.invalidate(["notifications"]);
  });
  source.addEventListener("resync", () => cache.invalidate([]));
  source.onerror = () => setStatus(source && source.readyState === EventSource.CLOSED ? "offline" : "reconnecting");
  return stopRealtime;
}

export function stopRealtime() {
  if (source) source.close();
  source = null;
  setStatus("connecting");
}
