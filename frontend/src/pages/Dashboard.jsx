import { ItemTable } from "../components/ItemTable.jsx";
import { Empty, ErrorBox, Spinner, TimeAgo } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { useQuery } from "../lib/hooks.js";
import { Link } from "../lib/router.js";
import { describeEvent } from "../logic/format.js";

const TILES = [
  { view: "my_work", title: "My open work", sort: "priority", empty: "Nothing assigned to you." , tone: "accent" },
  { view: "awaiting_my_approval", title: "Awaiting my approval", sort: "activity", empty: "No approvals waiting on you.", tone: "warn" },
  { view: "unassigned_urgent", title: "Unowned P1 / P2", sort: "priority", empty: "Every urgent item has an owner.", tone: "danger" },
  { view: "overdue", title: "Overdue", sort: "due", empty: "Nothing past its deadline.", tone: "danger" },
  { view: "stale", title: "Going stale", sort: "activity", empty: "No forgotten work.", hint: "Active, no activity in 3+ days", tone: "muted" },
  { view: "my_requests", title: "Requests I raised", sort: "activity", empty: "You have no open requests.", tone: "muted" },
];

function Tile({ tile, data }) {
  const count = data?.count ?? 0;
  return (
    <section className={`tile tile-${tile.tone}`}>
      <header className="tile-head">
        <div>
          <h3>{tile.title}</h3>
          {tile.hint && <div className="tile-hint">{tile.hint}</div>}
        </div>
        <Link className="tile-count" to={`/items?view=${tile.view}&sort=${tile.sort}`} title="View all">
          {data?.capped ? "999+" : count}
        </Link>
      </header>
      {count === 0 ? (
        <div className="tile-empty">{tile.empty}</div>
      ) : (
        <>
          <ItemTable items={data.items} compact />
          {count > data.items.length && (
            <Link className="tile-more" to={`/items?view=${tile.view}&sort=${tile.sort}`}>View all {data?.capped ? "999+" : count} →</Link>
          )}
        </>
      )}
    </section>
  );
}

export function DashboardPage({ me }) {
  const { data, error, loading, refetch } = useQuery(["dashboard"], () => api("/dashboard"));
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Good to see you, {me.name.split(" ")[0]}</h1>
          <p className="muted">What needs attention across {me.memberships.length ? me.memberships.map((m) => m.team.name).join(", ") : "your requests"}.</p>
        </div>
      </header>
      <ErrorBox error={error} onRetry={refetch} />
      {loading && !data ? (
        <Spinner />
      ) : data ? (
        <div className="dash">
          <div className="tiles">
            {TILES.map((t) => <Tile key={t.view} tile={t} data={data.tiles[t.view]} />)}
          </div>
          <section className="activity">
            <h3>Recent activity in your teams</h3>
            {data.recent_activity.length === 0 ? (
              <Empty title="No activity yet" />
            ) : (
              <ul className="feed">
                {data.recent_activity.map((ev) => (
                  <li key={ev.id}>
                    <span className="feed-actor">{ev.actor ? ev.actor.name : "System"}</span>{" "}
                    <span className="muted">{describeEvent(ev)}</span>{" "}
                    <Link to={`/items/${ev.item_id}`}>#{ev.item_id} {ev.item_title}</Link>
                    <div className="feed-time"><TimeAgo iso={ev.created_at} /></div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
