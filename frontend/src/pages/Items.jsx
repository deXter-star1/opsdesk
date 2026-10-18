import { useEffect, useState } from "react";
import { ItemTable } from "../components/ItemTable.jsx";
import { Empty, ErrorBox, Spinner } from "../components/ui.jsx";
import { api, qs } from "../lib/api.js";
import { useDebounced, useQuery } from "../lib/hooks.js";
import { cache } from "../lib/queryCache.js";
import { onItemEvent } from "../lib/realtime.js";
import { setSearchParams, useLocation } from "../lib/router.js";
import { KIND_LABEL, STATUS_LABEL, VIEW_LABEL } from "../logic/format.js";

const ACTIVE_STATUSES = ["open", "in_progress", "blocked", "awaiting_approval", "approved"];
const SORTS = { activity: "Recent activity", created: "Newest", priority: "Priority", due: "Due date" };

function readFilters(search) {
  const list = (k) => (search.get(k) ? search.get(k).split(",").filter(Boolean) : []);
  return {
    q: search.get("q") || "",
    view: search.get("view") || "",
    team: list("team"),
    status: list("status"),
    priority: list("priority"),
    kind: search.get("kind") || "",
    assignee: search.get("assignee") || "",
    state: search.get("state") || "active",
    sort: search.get("sort") || "activity",
  };
}

function toggle(list, value) {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

export function ItemsPage({ me }) {
  const { search } = useLocation();
  const filters = readFilters(search);
  const [text, setText] = useState(filters.q);
  const debounced = useDebounced(text, 300);
  const [liveCount, setLiveCount] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);

  // Filters live in the URL so views are shareable and survive reloads; defaults are omitted.
  const update = (patch) => {
    const next = { ...filters, ...patch };
    if (next.state === "active") next.state = "";
    if (next.sort === "activity") next.sort = "";
    setSearchParams(next);
  };

  useEffect(() => {
    if (debounced !== filters.q) update({ q: debounced });
  }, [debounced]);

  const key = ["items", filters];
  const params = { ...filters, limit: 30 };
  const { data, error, loading, refreshing, refetch } = useQuery(key, () =>
    api(`/items${qs(params)}`).then((page) => ({ pages: [page] })),
  );

  // Other people's changes: don't reshuffle the list under the user's cursor; offer a refresh.
  useEffect(() => onItemEvent(() => setLiveCount((n) => n + 1)), []);
  useEffect(() => setLiveCount(0), [JSON.stringify(filters)]);

  const loadMore = async () => {
    const last = data.pages[data.pages.length - 1];
    if (!last.next_cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api(`/items${qs({ ...params, cursor: last.next_cursor })}`);
      cache.setData(key, (d) => ({ pages: [...d.pages, page] }));
    } finally {
      setLoadingMore(false);
    }
  };

  const items = data ? data.pages.flatMap((p) => p.items) : [];
  const hasMore = data ? Boolean(data.pages[data.pages.length - 1].next_cursor) : false;
  const title = filters.view ? VIEW_LABEL[filters.view] : "All work";

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>{title}</h1>
          <p className="muted">Showing work you can see: your teams' items and requests you raised.</p>
        </div>
        {liveCount > 0 && (
          <button className="live-pill" onClick={() => { setLiveCount(0); refetch(); }}>
            {liveCount} update{liveCount > 1 ? "s" : ""} from others · Refresh
          </button>
        )}
      </header>

      <div className="filters">
        <input className="search" type="search" placeholder="Search titles and descriptions, or #id"
          value={text} onChange={(e) => setText(e.target.value)} aria-label="Search" />
        <select value={filters.view} onChange={(e) => update({ view: e.target.value })} aria-label="View">
          <option value="">All items</option>
          {Object.entries(VIEW_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <select value={filters.team[0] || ""} onChange={(e) => update({ team: e.target.value ? [e.target.value] : [] })} aria-label="Team">
          <option value="">All my teams</option>
          {me.memberships.map((m) => <option key={m.team.id} value={m.team.id}>{m.team.name}</option>)}
        </select>
        <select value={filters.kind} onChange={(e) => update({ kind: e.target.value })} aria-label="Type">
          <option value="">Any type</option>
          {Object.entries(KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <select value={filters.assignee} onChange={(e) => update({ assignee: e.target.value })} aria-label="Owner">
          <option value="">Any owner</option>
          <option value="me">Owned by me</option>
          <option value="none">Unowned</option>
        </select>
        <select value={filters.state} onChange={(e) => update({ state: e.target.value, status: [] })} aria-label="State">
          <option value="active">Active</option>
          <option value="closed">Closed</option>
          <option value="all">Active + closed</option>
        </select>
        <select value={filters.sort} onChange={(e) => update({ sort: e.target.value })} aria-label="Sort">
          {Object.entries(SORTS).map(([v, l]) => <option key={v} value={v}>Sort: {l}</option>)}
        </select>
      </div>
      <div className="chips">
        {[1, 2, 3, 4].map((p) => (
          <button key={p} className={`chip ${filters.priority.includes(String(p)) ? "on" : ""}`}
            onClick={() => update({ priority: toggle(filters.priority, String(p)) })}>P{p}</button>
        ))}
        <span className="chip-sep" />
        {(filters.state === "closed" ? ["resolved", "cancelled"] : ACTIVE_STATUSES).map((s) => (
          <button key={s} className={`chip ${filters.status.includes(s) ? "on" : ""}`}
            onClick={() => update({ status: toggle(filters.status, s) })}>{STATUS_LABEL[s]}</button>
        ))}
        {(filters.q || filters.view || filters.team.length || filters.status.length || filters.priority.length ||
          filters.kind || filters.assignee || filters.state !== "active") && (
          <button className="chip chip-clear" onClick={() => { setText(""); setSearchParams({}); }}>Clear filters</button>
        )}
        {refreshing && <span className="muted small">Updating…</span>}
      </div>

      <ErrorBox error={error} onRetry={refetch} />
      {loading && !data ? (
        <Spinner />
      ) : items.length === 0 && data ? (
        <Empty title="No matching work items">Try clearing a filter or searching for a different word.</Empty>
      ) : (
        <div className="card">
          <ItemTable items={items} />
          <div className="list-foot">
            <span className="muted small">{items.length} shown</span>
            {hasMore && (
              <button className="btn" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? "Loading…" : "Load more"}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
