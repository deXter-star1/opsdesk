import { useState } from "react";
import { api, newIdempotencyKey } from "../lib/api.js";
import { useQuery } from "../lib/hooks.js";
import { cache } from "../lib/queryCache.js";
import { toast } from "../lib/toasts.js";
import { describeEvent } from "../logic/format.js";
import { Avatar, Spinner, TimeAgo } from "./ui.jsx";

const PAGE = 50;

function mergeEvents(fresh, current) {
  // Refetches bring the newest page; keep any older pages the user already loaded.
  if (!current) return fresh;
  const seen = new Set(fresh.events.map((e) => e.id));
  const older = current.events.filter((e) => !seen.has(e.id) && e.id < (fresh.events.at(-1)?.id ?? Infinity));
  return { events: [...fresh.events, ...older], next_before: older.length ? current.next_before : fresh.next_before };
}

function EventRow({ ev }) {
  const d = ev.data || {};
  if (ev.kind === "commented") {
    return (
      <li className="ev ev-comment">
        <Avatar person={ev.actor} size={28} />
        <div className="ev-body">
          <div className="ev-head"><strong>{ev.actor?.name}</strong> <span className="muted"><TimeAgo iso={ev.created_at} /></span></div>
          <div className="comment-text">{d.body}</div>
        </div>
      </li>
    );
  }
  return (
    <li className={`ev ev-${ev.kind}`}>
      <span className="ev-dot" />
      <div className="ev-line">
        <strong>{ev.actor ? ev.actor.name : "System"}</strong> {describeEvent(ev)}
        {d.note && <span className="ev-note">“{d.note}”</span>}
        <span className="muted ev-time"> · <TimeAgo iso={ev.created_at} />{ev.item_version ? ` · v${ev.item_version}` : ""}</span>
      </div>
    </li>
  );
}

export function Timeline({ itemId, me, canComment }) {
  const key = ["events", itemId];
  const { data, loading } = useQuery(key, async () => {
    const fresh = await api(`/items/${itemId}/events?limit=${PAGE}`);
    return mergeEvents(fresh, cache.peek(key).data);
  });
  const [body, setBody] = useState("");
  const [pending, setPending] = useState([]); // optimistic comments: {tempId, body, key, failed}
  const [loadingOlder, setLoadingOlder] = useState(false);

  const loadOlder = async () => {
    setLoadingOlder(true);
    try {
      const page = await api(`/items/${itemId}/events?limit=${PAGE}&before=${data.next_before}`);
      cache.setData(key, (cur) => ({ events: [...cur.events, ...page.events], next_before: page.next_before }));
    } finally {
      setLoadingOlder(false);
    }
  };

  const send = async (entry) => {
    setPending((p) => p.map((x) => (x.tempId === entry.tempId ? { ...x, failed: false } : x)));
    try {
      // Same idempotency key on every retry of this comment: it can never be posted twice.
      const ev = await api(`/items/${itemId}/comments`, { method: "POST", body: { body: entry.body }, idempotencyKey: entry.key });
      cache.setData(key, (cur) => ({ ...cur, events: [ev, ...(cur?.events || []).filter((e) => e.id !== ev.id)] }));
      setPending((p) => p.filter((x) => x.tempId !== entry.tempId));
    } catch (e) {
      setPending((p) => p.map((x) => (x.tempId === entry.tempId ? { ...x, failed: true } : x)));
      toast(`Comment not sent: ${e.message}`, { kind: "error" });
    }
  };

  const submit = (e) => {
    e.preventDefault();
    const text = body.trim();
    if (!text) return;
    const entry = { tempId: `tmp-${Date.now()}`, body: text, key: newIdempotencyKey(), failed: false };
    setPending((p) => [...p, entry]);
    setBody("");
    send(entry);
  };

  const events = data ? [...data.events].reverse() : []; // server returns newest first
  return (
    <section className="timeline">
      <h3>History & discussion</h3>
      {loading && !data && <Spinner />}
      {data?.next_before && (
        <button className="btn btn-ghost btn-sm" onClick={loadOlder} disabled={loadingOlder}>
          {loadingOlder ? "Loading…" : "Load earlier history"}
        </button>
      )}
      <ul className="events">
        {events.map((ev) => <EventRow key={ev.id} ev={ev} />)}
        {pending.map((p) => (
          <li key={p.tempId} className={`ev ev-comment ev-pending ${p.failed ? "ev-failed" : ""}`}>
            <Avatar person={me} size={28} />
            <div className="ev-body">
              <div className="ev-head">
                <strong>{me.name}</strong>{" "}
                <span className="muted">{p.failed ? "Not sent" : "Sending…"}</span>
                {p.failed && <button className="btn btn-sm" onClick={() => send(p)}>Retry</button>}
              </div>
              <div className="comment-text">{p.body}</div>
            </div>
          </li>
        ))}
      </ul>
      {canComment && (
        <form className="composer" onSubmit={submit}>
          <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={3} maxLength={10000}
            placeholder="Add an update or ask a question. Watchers will be notified."
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e); }} />
          <div className="composer-foot">
            <span className="muted small">Ctrl/⌘ + Enter to send</span>
            <button className="btn btn-primary" disabled={!body.trim()}>Comment</button>
          </div>
        </form>
      )}
    </section>
  );
}
