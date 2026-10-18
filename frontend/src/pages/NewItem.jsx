import { useState } from "react";
import { ErrorBox, Spinner } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { useAction, useIntentKey, useQuery } from "../lib/hooks.js";
import { cache } from "../lib/queryCache.js";
import { navigate } from "../lib/router.js";
import { toast } from "../lib/toasts.js";
import { KIND_LABEL } from "../logic/format.js";

const APPROVAL_BY_DEFAULT = new Set(["payment", "compliance"]);
const SLA_HINT = { 1: "4 hours", 2: "1 day", 3: "3 days", 4: "7 days" };

export function NewItemPage({ me }) {
  const { data, error } = useQuery(["teams"], () => api("/teams"));
  const myTeams = new Set(me.memberships.map((m) => m.team.id));
  const [form, setForm] = useState({
    team_id: me.memberships[0]?.team.id || "", kind: "task", priority: 3, title: "", description: "",
    due_at: "", requires_approval: false,
  });
  const { pending, error: submitError, run } = useAction();
  const team = data?.teams.find((t) => t.id === Number(form.team_id));
  const isLead = team?.my_role === "lead";
  const approvalDefault = APPROVAL_BY_DEFAULT.has(form.kind);

  const payload = {
    team_id: Number(form.team_id), kind: form.kind, priority: Number(form.priority), title: form.title.trim(),
    description: form.description, due_at: form.due_at ? new Date(form.due_at).toISOString() : null,
    requires_approval: approvalDefault ? (isLead ? form.requires_approval : true) : form.requires_approval,
  };
  // Same key while the form content is unchanged: a double click, or a retry after a network
  // error, can never create two items. Editing the form starts a new intent.
  const intent = useIntentKey(payload);

  const set = (k) => (e) => {
    const v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    setForm((f) => {
      const next = { ...f, [k]: v };
      if (k === "kind") next.requires_approval = APPROVAL_BY_DEFAULT.has(v);
      return next;
    });
  };

  const submit = (e) => {
    e.preventDefault();
    run(async () => {
      const item = await api("/items", { method: "POST", body: payload, idempotencyKey: intent.get() });
      cache.setData(["item", item.id], item);
      cache.invalidate(["items"]);
      cache.invalidate(["dashboard"]);
      toast(`Created #${item.id}`, { kind: "success" });
      navigate(`/items/${item.id}`);
    }).catch(() => {});
  };

  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  return (
    <div className="page page-narrow">
      <header className="page-head"><h1>New work item</h1></header>
      <form className="card form" onSubmit={submit}>
        <div className="field-row">
          <label className="field">
            <span>Team queue</span>
            <select value={form.team_id} onChange={set("team_id")} required>
              <option value="" disabled>Choose a team</option>
              {data.teams.map((t) => (
                <option key={t.id} value={t.id}>{t.name}{myTeams.has(t.id) ? "" : " (request)"}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Type</span>
            <select value={form.kind} onChange={set("kind")}>
              {Object.entries(KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Priority</span>
            <select value={form.priority} onChange={set("priority")}>
              <option value={1}>P1 Critical</option>
              <option value={2}>P2 High</option>
              <option value={3}>P3 Normal</option>
              <option value={4}>P4 Low</option>
            </select>
          </label>
        </div>
        {team && !myTeams.has(team.id) && (
          <div className="notice">You are not in {team.name}. You will be the reporter: you can follow and comment, and their team will triage it.</div>
        )}
        <label className="field">
          <span>Title</span>
          <input value={form.title} onChange={set("title")} placeholder="What needs to happen?" minLength={3} maxLength={200} required autoFocus />
        </label>
        <label className="field">
          <span>Description</span>
          <textarea rows={6} value={form.description} onChange={set("description")} maxLength={20000}
            placeholder="Context, impact, links, what you've already tried." />
        </label>
        <div className="field-row">
          <label className="field">
            <span>Due (optional)</span>
            <input type="datetime-local" value={form.due_at} onChange={set("due_at")} />
            <small className="muted">Defaults to P{form.priority} SLA: {SLA_HINT[form.priority]}</small>
          </label>
          <label className="check">
            <input type="checkbox" checked={payload.requires_approval} onChange={set("requires_approval")}
              disabled={approvalDefault && !isLead} />
            Requires lead approval before resolving
            {approvalDefault && !isLead && <small className="muted"> (always on for {KIND_LABEL[form.kind]})</small>}
          </label>
        </div>
        <ErrorBox error={submitError} />
        <div className="form-foot">
          <button type="button" className="btn" onClick={() => history.back()}>Cancel</button>
          <button className="btn btn-primary" disabled={pending || !form.team_id || form.title.trim().length < 3}>
            {pending ? "Creating…" : "Create work item"}
          </button>
        </div>
      </form>
    </div>
  );
}
