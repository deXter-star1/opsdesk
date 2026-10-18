import { Link, navigate } from "../lib/router.js";
import { DueLabel, KindTag, Person, PriorityBadge, StatusPill, TeamTag, TimeAgo } from "./ui.jsx";

export function ItemTable({ items, compact = false }) {
  return (
    <table className={`items ${compact ? "items-compact" : ""}`}>
      {!compact && (
        <thead>
          <tr>
            <th className="col-id">ID</th>
            <th className="col-p">Pri</th>
            <th>Work item</th>
            <th className="col-status">Status</th>
            <th className="col-owner">Owner</th>
            <th className="col-due">Due</th>
            <th className="col-act">Activity</th>
          </tr>
        </thead>
      )}
      <tbody>
        {items.map((it) => (
          <tr key={it.id} onClick={(e) => { if (!e.target.closest("a")) navigate(`/items/${it.id}`); }}
            className={it.overdue ? "row-overdue" : ""}>
            {!compact && <td className="col-id mono">#{it.id}</td>}
            <td className="col-p"><PriorityBadge p={it.priority} /></td>
            <td className="col-title">
              <Link to={`/items/${it.id}`} className="item-link">{it.title}</Link>
              <div className="row-meta">
                {compact && <span className="mono muted">#{it.id}</span>}
                <TeamTag team={it.team} />
                <KindTag kind={it.kind} />
                {it.requires_approval && <span className="tag-approval" title="Requires approval">approval</span>}
                {it.sla_breached && it.status !== "resolved" && it.status !== "cancelled" && (
                  <span className="tag-sla">SLA breached</span>
                )}
                {compact && <span className="meta-due">{it.overdue ? "" : "due "}<DueLabel item={it} /></span>}
              </div>
            </td>
            <td className="col-status"><StatusPill status={it.status} /></td>
            {!compact && <td className="col-owner"><Person person={it.assignee} /></td>}
            {!compact && <td className="col-due"><DueLabel item={it} /></td>}
            {!compact && <td className="col-act muted"><TimeAgo iso={it.last_activity_at} /></td>}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
