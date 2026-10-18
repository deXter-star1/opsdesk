import { useEffect, useRef, useState } from "react";
import { EditItem } from "../components/EditItem.jsx";
import { Timeline } from "../components/Timeline.jsx";
import {
  DueLabel, ErrorBox, KindTag, Modal, Person, PriorityBadge, Spinner, StatusPill, TeamTag, TimeAgo,
} from "../components/ui.jsx";
import { api, newIdempotencyKey } from "../lib/api.js";
import { useAction, useQuery } from "../lib/hooks.js";
import { assignItem, claimItem, moveItem, setWatching, transitionItem } from "../lib/itemMutations.js";
import { onItemEvent } from "../lib/realtime.js";
import { Link, navigate } from "../lib/router.js";
import { toast } from "../lib/toasts.js";
import { ACTION_LABEL, DONE_LABEL, describeEvent, describeLive, fullTime, NOTE_PROMPT, NOTE_REQUIRED } from "../logic/format.js";

const PRIMARY_ORDER = ["claim", "start", "unblock", "submit_for_approval", "approve", "resolve", "reopen"];
const SECONDARY_ORDER = ["block", "reject", "cancel"];

function StaleNotice({ stale, onDismiss }) {
  return (
    <div className="notice notice-warn">
      <strong>This item changed while you were looking at it, so nothing was done.</strong>
      <ul>
        {(stale.changes_since || []).map((e) => (
          <li key={e.id}>{e.actor ? e.actor.name : "System"} {describeEvent(e)} <span className="muted">(v{e.item_version})</span></li>
        ))}
      </ul>
      <span>The page now shows the latest version (v{stale.current_version}). Review it and try again if it still makes sense.</span>
      <button className="btn btn-sm" onClick={onDismiss}>OK</button>
    </div>
  );
}

function NoteDialog({ action, onSubmit, onClose, pending }) {
  const [note, setNote] = useState("");
  return (
    <Modal title={ACTION_LABEL[action]} onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Back</button>
          <button className={`btn ${action === "reject" || action === "cancel" ? "btn-danger" : "btn-primary"}`}
            disabled={!note.trim() || pending} onClick={() => onSubmit(note)}>
            {pending ? "Working…" : ACTION_LABEL[action]}
          </button>
        </>
      }>
      <label className="field">
        <span>{NOTE_PROMPT[action]}</span>
        <textarea autoFocus rows={4} value={note} onChange={(e) => setNote(e.target.value)} maxLength={5000} />
      </label>
    </Modal>
  );
}

function AssignDialog({ item, me, onClose }) {
  const { data } = useQuery(["members", item.team.id], () => api(`/teams/${item.team.id}/members`));
  const { pending, run } = useAction();
  const eligible = (data?.members || []).filter((m) => m.role !== "viewer");
  const choose = (id) =>
    run(async () => {
      const res = await assignItem(item, id);
      if (!res.ok) toast("The item changed meanwhile. Showing the latest version; try again.", { kind: "warn" });
      else toast(id ? "Owner updated" : "Owner removed", { kind: "success" });
      onClose();
    }).catch((e) => toast(e.message, { kind: "error" }));
  return (
    <Modal title={`Assign #${item.id}`} onClose={onClose}>
      {!data ? <Spinner /> : (
        <ul className="pick">
          {eligible.map((m) => (
            <li key={m.id}>
              <button disabled={pending || item.assignee?.id === m.id} onClick={() => choose(m.id)}>
                <Person person={m} /> <span className="muted small">{m.role}{m.id === me.id ? " · you" : ""}</span>
                {item.assignee?.id === m.id && <span className="muted small"> · current owner</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function MoveDialog({ item, onClose }) {
  const { data } = useQuery(["teams"], () => api("/teams"));
  const { pending, run } = useAction();
  const choose = (team) =>
    run(async () => {
      const out = await moveItem(item, team.id);
      onClose();
      if (out.visible === false) {
        toast(`Moved to ${team.name}. You no longer have access to it.`, { kind: "info", timeout: 7000 });
        navigate("/items");
      } else toast(`Moved to ${team.name}; it is now unowned in their queue`, { kind: "success" });
    }).catch((e) => toast(e.message, { kind: "error" }));
  return (
    <Modal title={`Move #${item.id} to another team`} onClose={onClose}>
      <p className="muted">The item keeps its history. Its owner is cleared so the receiving team can triage it.</p>
      {!data ? <Spinner /> : (
        <ul className="pick">
          {data.teams.filter((t) => t.id !== item.team.id).map((t) => (
            <li key={t.id}><button disabled={pending} onClick={() => choose(t)}><strong>{t.key}</strong> {t.name}</button></li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function ActionBar({ item, me, onEdit }) {
  const [noteFor, setNoteFor] = useState(null);
  const [dialog, setDialog] = useState(null);
  const [stale, setStale] = useState(null);
  const { pending, run } = useAction();
  const keyRef = useRef({}); // one idempotency key per (action, version): retries reuse it
  const allowed = new Set(item.allowed_actions || []);

  const doTransition = (action, note) =>
    run(async () => {
      const k = `${action}:${item.version}`;
      keyRef.current[k] ||= newIdempotencyKey();
      const res = await transitionItem(item, action, note, keyRef.current[k]);
      setNoteFor(null);
      if (!res.ok) setStale(res.stale);
      else toast(DONE_LABEL[action] || "Done", { kind: "success" });
    }).catch((e) => toast(e.message, { kind: "error", timeout: 7000 }));

  const click = (action) => {
    if (action === "claim") return run(() => claimItem(item, me)).catch(() => {});
    if (NOTE_REQUIRED.has(action)) return setNoteFor(action);
    return doTransition(action, null);
  };

  const primary = PRIMARY_ORDER.filter((a) => allowed.has(a));
  const secondary = SECONDARY_ORDER.filter((a) => allowed.has(a));
  return (
    <>
      {stale && <StaleNotice stale={stale} onDismiss={() => setStale(null)} />}
      <div className="actionbar">
        {primary.map((a) => (
          <button key={a} className={`btn ${a === "approve" || a === "claim" ? "btn-primary" : ""}`} disabled={pending || item._pending}
            onClick={() => click(a)}>{ACTION_LABEL[a]}</button>
        ))}
        {allowed.has("assign") && <button className="btn" disabled={pending} onClick={() => setDialog("assign")}>Assign…</button>}
        {allowed.has("unassign") && !allowed.has("assign") && (
          <button className="btn" disabled={pending} onClick={() => run(() => assignItem(item, null)).catch((e) => toast(e.message, { kind: "error" }))}>Release</button>
        )}
        {allowed.has("edit") && <button className="btn" onClick={onEdit}>Edit</button>}
        <span className="actionbar-spacer" />
        {secondary.map((a) => (
          <button key={a} className="btn btn-ghost" disabled={pending} onClick={() => click(a)}>{ACTION_LABEL[a]}</button>
        ))}
        {allowed.has("move") && <button className="btn btn-ghost" onClick={() => setDialog("move")}>Move team…</button>}
        <button className="btn btn-ghost" onClick={() => setWatching(item, !item.watching)}>
          {item.watching ? "Unwatch" : "Watch"} · {item.watcher_count}
        </button>
      </div>
      {item.allowed_actions?.length === 1 && (
        <p className="muted small">You can follow and comment on this item. Changes are made by the {item.team.name} team.</p>
      )}
      {noteFor && <NoteDialog action={noteFor} pending={pending} onClose={() => setNoteFor(null)} onSubmit={(n) => doTransition(noteFor, n)} />}
      {dialog === "assign" && <AssignDialog item={item} me={me} onClose={() => setDialog(null)} />}
      {dialog === "move" && <MoveDialog item={item} onClose={() => setDialog(null)} />}
    </>
  );
}

export function ItemDetailPage({ id, me }) {
  const itemId = Number(id);
  const { data: item, error, loading, refetch } = useQuery(["item", itemId], () => api(`/items/${itemId}`));
  const [editing, setEditing] = useState(false);
  const [liveNote, setLiveNote] = useState(null);

  useEffect(() => {
    setEditing(false);
    return onItemEvent((ev) => {
      if (ev.item_id !== itemId || ev.actor_id === me.id) return;
      setLiveNote({ name: ev.actor_name || "System", text: describeLive(ev), at: ev.created_at });
    });
  }, [itemId]);

  if (loading && !item) return <div className="page"><Spinner /></div>;
  if (error && !item) {
    return (
      <div className="page">
        <ErrorBox error={error} onRetry={refetch} />
        <Link to="/items">← Back to work items</Link>
      </div>
    );
  }
  if (!item) return null;
  const closed = item.status === "resolved" || item.status === "cancelled";

  return (
    <div className="page page-detail">
      <div className="crumbs">
        <Link to="/items">Work items</Link> <span>/</span> <span className="mono">#{item.id}</span>
      </div>
      {liveNote && (
        <div className="notice notice-live" onClick={() => setLiveNote(null)}>
          <strong>{liveNote.name}</strong> just {liveNote.text}. {editing ? "Your draft is untouched." : "Showing the latest version."}
        </div>
      )}
      <div className="detail">
        <div className="detail-main">
          <div className="detail-badges">
            <PriorityBadge p={item.priority} long />
            <StatusPill status={item.status} />
            <KindTag kind={item.kind} />
            {item.requires_approval && <span className="tag-approval">approval required</span>}
            {item.sla_breached && !closed && <span className="tag-sla">SLA breached</span>}
            {item._pending && <span className="muted small">saving…</span>}
          </div>
          {editing ? (
            <EditItem item={item} canEditApproval={item.allowed_actions.includes("edit_approval_flag")} onDone={() => setEditing(false)} />
          ) : (
            <>
              <h1 className="detail-title">{item.title}</h1>
              <ActionBar item={item} me={me} onEdit={() => { setLiveNote(null); setEditing(true); }} />
              {item.resolution && (
                <div className="resolution">
                  <strong>{item.status === "resolved" ? "Resolution" : "Cancelled"}:</strong> {item.resolution}
                </div>
              )}
              <div className="description">{item.description || <span className="muted">No description.</span>}</div>
            </>
          )}
          <Timeline itemId={item.id} me={me} canComment={item.allowed_actions.includes("comment")} />
        </div>
        <aside className="detail-side">
          <dl>
            <dt>Team</dt><dd><TeamTag team={item.team} /> {item.team.name}</dd>
            <dt>Owner</dt><dd><Person person={item.assignee} /></dd>
            <dt>Reporter</dt><dd><Person person={item.reporter} /></dd>
            <dt>Due</dt><dd><DueLabel item={item} />{item.due_at && <div className="muted small">{fullTime(item.due_at)}</div>}</dd>
            {item.requires_approval && (
              <>
                <dt>Approval</dt>
                <dd>
                  {item.approved_by ? <>Approved by {item.approved_by.name} <span className="muted small"><TimeAgo iso={item.approved_at} /></span></>
                    : item.approval_requested_by ? <>Requested by {item.approval_requested_by.name}</>
                    : <span className="muted">Not yet requested</span>}
                </dd>
              </>
            )}
            <dt>Created</dt><dd><TimeAgo iso={item.created_at} /></dd>
            <dt>Last change</dt><dd><TimeAgo iso={item.updated_at} /></dd>
            <dt>Version</dt><dd className="mono">v{item.version}</dd>
            <dt>Your role</dt><dd>{item.my_role || (item.reporter.id === me.id ? "Reporter" : "—")}</dd>
          </dl>
        </aside>
      </div>
    </div>
  );
}
