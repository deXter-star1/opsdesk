export const STATUS_LABEL = {
  open: "Open",
  in_progress: "In progress",
  blocked: "Blocked",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  resolved: "Resolved",
  cancelled: "Cancelled",
};

export const KIND_LABEL = {
  incident: "Incident",
  customer_issue: "Customer issue",
  engineering: "Engineering",
  payment: "Payment",
  compliance: "Compliance",
  task: "Task",
};

export const PRIORITY_LABEL = { 1: "P1 Critical", 2: "P2 High", 3: "P3 Normal", 4: "P4 Low" };

export const ACTION_LABEL = {
  claim: "Claim",
  start: "Start work",
  block: "Mark blocked",
  unblock: "Unblock",
  submit_for_approval: "Request approval",
  approve: "Approve",
  reject: "Reject",
  resolve: "Resolve",
  cancel: "Cancel item",
  reopen: "Reopen",
};

export const DONE_LABEL = {
  start: "Work started",
  block: "Marked blocked",
  unblock: "Unblocked",
  submit_for_approval: "Approval requested",
  approve: "Approved",
  reject: "Rejected; sent back to the owner",
  resolve: "Resolved",
  cancel: "Cancelled",
  reopen: "Reopened",
};

export const NOTE_REQUIRED = new Set(["block", "reject", "resolve", "cancel", "reopen"]);

export const NOTE_PROMPT = {
  block: "What is it blocked on?",
  reject: "Why is this being rejected? The owner will see this.",
  resolve: "Resolution: what was done?",
  cancel: "Why is this being cancelled?",
  reopen: "Why does this need to be reopened?",
};

export const VIEW_LABEL = {
  my_work: "My open work",
  awaiting_my_approval: "Awaiting my approval",
  unassigned_urgent: "Unowned P1/P2",
  overdue: "Overdue",
  stale: "Going stale (no activity 3d+)",
  my_requests: "My requests",
  watching: "Watching",
};

export const REASON_LABEL = {
  assigned_to_you: "assigned you",
  unassigned_from_you: "took you off",
  ownership_changed: "changed the owner of",
  approval_requested: "needs your approval on",
  approval_decided: "made an approval decision on",
  status_changed: "changed the status of",
  new_comment: "commented on",
  escalated: "escalated to P1",
  moved_to_your_team: "moved to your team",
  urgent_item_created: "raised an urgent item",
  sla_breached: "SLA breached on",
};

export function relTime(iso, now = Date.now()) {
  if (!iso) return "";
  const diff = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(diff);
  const fmt = (n, unit) => {
    const v = Math.round(n);
    return diff < 0 ? `${v}${unit} ago` : `in ${v}${unit}`;
  };
  if (abs < 45) return diff < 0 ? "just now" : "in a moment";
  if (abs < 3600) return fmt(abs / 60, "m");
  if (abs < 86400) return fmt(abs / 3600, "h");
  if (abs < 86400 * 30) return fmt(abs / 86400, "d");
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function fullTime(iso) {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
}

export function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const FIELD_LABEL = { title: "title", description: "description", priority: "priority", kind: "type",
  due_at: "due date", requires_approval: "approval requirement" };

function fieldValue(field, v) {
  if (field === "priority") return `P${v}`;
  if (field === "kind") return KIND_LABEL[v] || v;
  if (field === "due_at") return v ? fullTime(v) : "none";
  if (field === "requires_approval") return v ? "required" : "not required";
  return null;
}

// One-line human description of a history event (actor name is rendered separately).
export function describeEvent(ev) {
  const d = ev.data || {};
  switch (ev.kind) {
    case "created":
      return "created this item";
    case "assigned":
      if (d.via === "claim") return "took ownership and started work";
      if (d.via === "unassign") return "removed the owner";
      return `assigned it to ${d.to_name || "someone"}`;
    case "updated": {
      const parts = Object.entries(d.changes || {}).map(([f, [from, to]]) => {
        const a = fieldValue(f, from);
        const b = fieldValue(f, to);
        if (a && b) return `${FIELD_LABEL[f]} ${a} → ${b}`;
        if (f === "title") return `title to “${to}”`;
        return FIELD_LABEL[f] || f;
      });
      return `changed ${parts.join(", ")}${d.merged_over_version ? " (merged with a concurrent edit)" : ""}`;
    }
    case "status_changed": {
      const verb = {
        start: "started work", block: "marked it blocked", unblock: "unblocked it",
        submit_for_approval: "requested approval", approve: "approved it", reject: "rejected the approval request",
        resolve: "resolved it", cancel: "cancelled it", reopen: "reopened it",
      }[d.action] || `moved it to ${STATUS_LABEL[d.to] || d.to}`;
      return verb;
    }
    case "moved":
      return `moved it from ${d.from_team?.name} to ${d.to_team?.name}`;
    case "commented":
      return "commented";
    case "sla_breached":
      return "SLA deadline passed";
    default:
      return ev.kind;
  }
}

// Short phrase for a realtime hint (the stream carries a summary, not the full event).
export function describeLive(ev) {
  switch (ev.kind) {
    case "updated":
      return `changed ${(ev.fields || []).map((f) => FIELD_LABEL[f] || f).join(", ") || "this item"}`;
    case "status_changed":
      return `moved it to ${STATUS_LABEL[ev.to] || ev.to}`;
    case "assigned":
      return ev.to_name ? `assigned it to ${ev.to_name}` : "removed the owner";
    case "commented":
      return "commented";
    case "moved":
      return "moved it to another team";
    case "sla_breached":
      return "flagged that the SLA deadline passed";
    default:
      return "updated this item";
  }
}
