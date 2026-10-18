import { useEffect, useState } from "react";
import { fullTime, KIND_LABEL, relTime, STATUS_LABEL } from "../logic/format.js";

export function PriorityBadge({ p, long = false }) {
  const label = long ? { 1: "P1 Critical", 2: "P2 High", 3: "P3 Normal", 4: "P4 Low" }[p] : `P${p}`;
  return <span className={`prio prio-${p}`} title={`Priority ${p}`}>{label}</span>;
}

export function StatusPill({ status }) {
  return <span className={`status status-${status}`}>{STATUS_LABEL[status] || status}</span>;
}

export function KindTag({ kind }) {
  return <span className="kind">{KIND_LABEL[kind] || kind}</span>;
}

export function TeamTag({ team }) {
  return <span className="team-tag" title={team.name}>{team.key}</span>;
}

function initials(name = "?") {
  return name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
}

function hue(id) {
  return ((id || 0) * 47) % 360;
}

export function Avatar({ person, size = 22 }) {
  if (!person) return <span className="avatar avatar-empty" style={{ width: size, height: size }} />;
  return (
    <span className="avatar" title={person.name}
      style={{ width: size, height: size, fontSize: size * 0.42, background: `hsl(${hue(person.id)} 45% 42%)` }}>
      {initials(person.name)}
    </span>
  );
}

export function Person({ person, empty = "Unassigned" }) {
  if (!person) return <span className="muted">{empty}</span>;
  return (
    <span className="person">
      <Avatar person={person} />
      <span>{person.name}</span>
    </span>
  );
}

// Re-renders every 30s so "5m ago" stays truthful on screens left open.
export function TimeAgo({ iso }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30000);
    return () => clearInterval(t);
  }, []);
  if (!iso) return null;
  return <time dateTime={iso} title={fullTime(iso)}>{relTime(iso)}</time>;
}

export function DueLabel({ item }) {
  if (!item.due_at) return <span className="muted">—</span>;
  const closed = item.status === "resolved" || item.status === "cancelled";
  return (
    <span className={item.overdue && !closed ? "due overdue" : "due"} title={fullTime(item.due_at)}>
      {item.overdue && !closed ? "Overdue " : ""}
      {relTime(item.due_at)}
    </span>
  );
}

export function Spinner({ label = "Loading" }) {
  return <div className="spinner" role="status" aria-label={label}><span /></div>;
}

export function ErrorBox({ error, onRetry }) {
  if (!error) return null;
  return (
    <div className="error-box" role="alert">
      <strong>{error.status === 404 ? "Not found" : "Something went wrong"}</strong>
      <span>{error.message}</span>
      {onRetry && <button className="btn btn-sm" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function Empty({ title, children }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

export function Modal({ title, onClose, children, footer, wide = false }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose?.();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
