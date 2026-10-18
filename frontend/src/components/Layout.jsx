import { useState } from "react";
import { api, isFlakyNetwork, setFlakyNetwork } from "../lib/api.js";
import { cache } from "../lib/queryCache.js";
import { useRealtimeStatus } from "../lib/realtime.js";
import { Link, navigate, useLocation } from "../lib/router.js";
import { dismiss, useToasts } from "../lib/toasts.js";
import { Avatar } from "./ui.jsx";

const NAV = [
  { to: "/", label: "Dashboard", match: (p) => p === "/" },
  { to: "/items", label: "All work", match: (p, s) => p === "/items" && !s.get("view") },
  { to: "/items?view=my_work&sort=priority", label: "My work", match: (p, s) => s.get("view") === "my_work" },
  { to: "/items?view=awaiting_my_approval", label: "Approvals", match: (p, s) => s.get("view") === "awaiting_my_approval" },
  { to: "/items?view=watching", label: "Watching", match: (p, s) => s.get("view") === "watching" },
  { to: "/notifications", label: "Notifications", match: (p) => p === "/notifications", badge: true },
];

function LiveDot() {
  const status = useRealtimeStatus();
  const label = { live: "Live", reconnecting: "Reconnecting…", connecting: "Connecting…", offline: "Offline" }[status];
  return (
    <span className={`live-dot live-${status}`} title="Live updates from other users">
      <span className="dot" />
      {label}
    </span>
  );
}

function Toasts() {
  const toasts = useToasts();
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => dismiss(t.id)}>{t.message}</div>
      ))}
    </div>
  );
}

export function Layout({ me, children }) {
  const { pathname, search } = useLocation();
  const [flaky, setFlaky] = useState(isFlakyNetwork());
  const logout = async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    cache.clear();
    navigate("/");
    location.reload();
  };
  return (
    <div className="shell">
      <aside className="sidebar">
        <Link to="/" className="brand">
          <span className="brand-mark">✓</span> OpsDesk
        </Link>
        <Link to="/items/new" className="btn btn-primary new-btn">+ New work item</Link>
        <nav>
          {NAV.map((n) => (
            <Link key={n.to} to={n.to} className={`nav-link ${n.match(pathname, search) ? "active" : ""}`}>
              {n.label}
              {n.badge && me.unread_notifications > 0 && (
                <span className="nav-badge">{me.unread_notifications >= 100 ? "99+" : me.unread_notifications}</span>
              )}
            </Link>
          ))}
          {me.is_admin && (
            <Link to="/admin/jobs" className={`nav-link ${pathname === "/admin/jobs" ? "active" : ""}`}>
              Background jobs
            </Link>
          )}
        </nav>
        <div className="sidebar-foot">
          <LiveDot />
          <label className="chaos" title="Drop the first response of every change, as if the network failed after the server processed it. Retries reuse the same idempotency key.">
            <input type="checkbox" checked={flaky} onChange={(e) => { setFlakyNetwork(e.target.checked); setFlaky(e.target.checked); }} />
            Simulate flaky network
          </label>
          <div className="me">
            <Avatar person={me} size={28} />
            <div className="me-text">
              <div className="me-name">{me.name}</div>
              <div className="me-teams">
                {me.memberships.length
                  ? me.memberships.map((m) => `${m.team.key} ${m.role}`).join(" · ")
                  : "No team (requester)"}
              </div>
            </div>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="main">{children}</main>
      <Toasts />
    </div>
  );
}
