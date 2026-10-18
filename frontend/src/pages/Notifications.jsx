import { useState } from "react";
import { Empty, ErrorBox, Spinner, TimeAgo } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { useQuery } from "../lib/hooks.js";
import { cache } from "../lib/queryCache.js";
import { navigate } from "../lib/router.js";
import { REASON_LABEL } from "../logic/format.js";

export function NotificationsPage() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const key = ["notifications", unreadOnly];
  const { data, error, loading, refetch } = useQuery(key, () => api(`/notifications${unreadOnly ? "?unread=1" : ""}`));
  const [older, setOlder] = useState([]);
  const [cursor, setCursor] = useState(null);

  const markRead = async (body) => {
    const out = await api("/notifications/read", { method: "POST", body });
    cache.setData(["me"], (me) => (me ? { ...me, unread_notifications: out.unread } : me));
    cache.invalidate(["notifications"]);
  };

  const open = async (n) => {
    if (!n.read) markRead({ ids: [n.id] }).catch(() => {});
    navigate(`/items/${n.item_id}`);
  };

  const loadMore = async () => {
    const before = cursor || data.next_before;
    const page = await api(`/notifications?before=${before}${unreadOnly ? "&unread=1" : ""}`);
    setOlder((o) => [...o, ...page.notifications]);
    setCursor(page.next_before);
  };

  const list = data ? [...data.notifications, ...older] : [];
  const more = older.length ? cursor : data?.next_before;
  return (
    <div className="page page-narrow">
      <header className="page-head">
        <h1>Notifications</h1>
        <div className="head-actions">
          <label className="check"><input type="checkbox" checked={unreadOnly} onChange={(e) => { setOlder([]); setCursor(null); setUnreadOnly(e.target.checked); }} /> Unread only</label>
          <button className="btn" onClick={() => markRead({ all: true })}>Mark all read</button>
        </div>
      </header>
      <ErrorBox error={error} onRetry={refetch} />
      {loading && !data ? <Spinner /> : list.length === 0 ? (
        <Empty title="You're all caught up">Notifications appear when work is assigned to you, needs your approval, or changes on items you watch.</Empty>
      ) : (
        <ul className="notifs card">
          {list.map((n) => (
            <li key={n.id} className={n.read ? "" : "unread"} onClick={() => open(n)}>
              <div>
                <strong>{n.actor_name || "System"}</strong> {REASON_LABEL[n.reason] || n.reason}{" "}
                <span className="mono">#{n.item_id}</span> {n.item_title}
              </div>
              <div className="muted small"><TimeAgo iso={n.created_at} /></div>
            </li>
          ))}
        </ul>
      )}
      {more && <button className="btn" onClick={loadMore}>Load older</button>}
    </div>
  );
}
