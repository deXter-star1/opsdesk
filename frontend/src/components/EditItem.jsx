import { useState } from "react";
import { ApiError } from "../lib/api.js";
import { useIntentKey } from "../lib/hooks.js";
import { saveEdits } from "../lib/itemMutations.js";
import { toast } from "../lib/toasts.js";
import { KIND_LABEL, fullTime, toLocalInput } from "../logic/format.js";
import { acceptTheirs, conflictRows, diffDraft } from "../logic/items.js";
import { Modal } from "./ui.jsx";

function show(field, v) {
  if (v === null || v === undefined || v === "") return <span className="muted">empty</span>;
  if (field === "priority") return `P${v}`;
  if (field === "kind") return KIND_LABEL[v];
  if (field === "due_at") return fullTime(v);
  if (field === "requires_approval") return v ? "Required" : "Not required";
  if (field === "description") return <span className="clip">{String(v)}</span>;
  return String(v);
}

function ConflictDialog({ details, onKeepMine, onUseTheirs, onClose }) {
  const rows = conflictRows(details);
  const who = [...new Set((details.changes_since || []).map((e) => e.actor?.name).filter(Boolean))].join(", ");
  return (
    <Modal wide title="Someone else changed this while you were editing" onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Keep editing</button>
          <button className="btn" onClick={onUseTheirs}>Use their values</button>
          <button className="btn btn-danger" onClick={onKeepMine}>Overwrite with mine</button>
        </>
      }>
      <p>
        {who || "Another user"} saved version {details.current_version} after you started from version{" "}
        {details.expected_version}. Nothing of yours has been saved yet. Fields that clash are highlighted.
      </p>
      <table className="conflict">
        <thead><tr><th>Field</th><th>Your edit</th><th>Current value</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.field} className={r.conflicting ? "clash" : ""}>
              <td>{r.field.replace("_", " ")}{r.conflicting && <span className="clash-tag">clash</span>}</td>
              <td>{show(r.field, r.yours)}</td>
              <td>{show(r.field, r.theirs)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}

export function EditItem({ item, canEditApproval, onDone }) {
  // `base` is the version the user started editing from. It only moves forward when the user
  // explicitly accepts newer server state, never silently while they type.
  const [base, setBase] = useState(item);
  const [draft, setDraft] = useState({
    title: item.title, description: item.description, priority: item.priority, kind: item.kind,
    due_at: toLocalInput(item.due_at), requires_approval: item.requires_approval,
  });
  const [conflict, setConflict] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const changes = diffDraft(base, draft);
  const intent = useIntentKey({ v: base.version, changes });
  const set = (k) => (e) => setDraft((d) => ({ ...d, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  const save = async (fromVersion = base.version, payload = changes) => {
    if (!Object.keys(payload).length) return onDone();
    setSaving(true);
    setError(null);
    try {
      const out = await saveEdits(item.id, fromVersion, payload, intent.get());
      intent.reset();
      if (out.merged) toast("Saved. Merged with another person's change to different fields.", { kind: "success", timeout: 6000 });
      else toast("Saved", { kind: "success" });
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.code === "edit_conflict") setConflict(e.details);
      else setError(e);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="edit">
      {item.version > base.version && !conflict && (
        <div className="notice notice-warn">
          This item changed (v{base.version} → v{item.version}) since you started editing. Saving will merge your
          edits if they touch different fields, or ask you what to do.
        </div>
      )}
      <label className="field">
        <span>Title</span>
        <input value={draft.title} onChange={set("title")} maxLength={200} minLength={3} required />
      </label>
      <label className="field">
        <span>Description</span>
        <textarea rows={6} value={draft.description} onChange={set("description")} maxLength={20000} />
      </label>
      <div className="field-row">
        <label className="field">
          <span>Priority</span>
          <select value={draft.priority} onChange={set("priority")}>
            {[1, 2, 3, 4].map((p) => <option key={p} value={p}>P{p}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Type</span>
          <select value={draft.kind} onChange={set("kind")}>
            {Object.entries(KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Due</span>
          <input type="datetime-local" value={draft.due_at} onChange={set("due_at")} />
        </label>
      </div>
      {canEditApproval && (
        <label className="check">
          <input type="checkbox" checked={draft.requires_approval} onChange={set("requires_approval")} />
          Requires lead approval before it can be resolved
        </label>
      )}
      {error && <div className="notice notice-error">{error.message}</div>}
      <div className="edit-foot">
        <span className="muted small">Editing from v{base.version} · {Object.keys(changes).length} field(s) changed</span>
        <button className="btn" onClick={onDone} disabled={saving}>Cancel</button>
        <button className="btn btn-primary" onClick={() => save()} disabled={saving || draft.title.trim().length < 3}>
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
      {conflict && (
        <ConflictDialog
          details={conflict}
          onClose={() => setConflict(null)}
          onKeepMine={() => {
            // Deliberate overwrite: re-send the same edits against the version we now know about.
            const d = conflict;
            setConflict(null);
            setBase(d.item);
            save(d.current_version, d.your_changes);
          }}
          onUseTheirs={() => {
            const d = conflict;
            setConflict(null);
            setBase(d.item);
            setDraft((cur) => {
              const next = acceptTheirs(cur, d);
              if ("due_at" in next && (d.conflicting_fields || []).includes("due_at")) next.due_at = toLocalInput(d.item.due_at);
              return next;
            });
          }}
        />
      )}
    </div>
  );
}
