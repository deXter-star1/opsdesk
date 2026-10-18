import { ErrorBox, Spinner, TimeAgo } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { useQuery } from "../lib/hooks.js";
import { toast } from "../lib/toasts.js";

export function AdminJobsPage() {
  const { data, error, refetch } = useQuery(["admin", "jobs"], () => api("/admin/jobs"), { refetchIntervalMs: 3000 });
  const retry = async (id) => {
    try {
      await api(`/admin/jobs/${id}/retry`, { method: "POST" });
      toast(`Job ${id} re-queued`, { kind: "success" });
      refetch();
    } catch (e) {
      toast(e.message, { kind: "error" });
    }
  };
  if (error) return <div className="page"><ErrorBox error={error} onRetry={refetch} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const s = data.stats;
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Background jobs</h1>
          <p className="muted">Notification fan-out, SLA sweeps and maintenance run in the worker, after the user's action has committed. Refreshes every 3s.</p>
        </div>
      </header>
      <div className="stats">
        {["pending", "running", "done", "dead"].map((k) => (
          <div key={k} className={`stat stat-${k}`}><div className="stat-n">{s.by_status[k]}</div><div className="stat-l">{k}</div></div>
        ))}
        <div className="stat"><div className="stat-n">{s.retrying}</div><div className="stat-l">retrying</div></div>
        <div className="stat"><div className="stat-n small">{s.oldest_ready_at ? <TimeAgo iso={s.oldest_ready_at} /> : "—"}</div><div className="stat-l">oldest ready</div></div>
      </div>
      <h3>Failed and dead-lettered jobs</h3>
      {data.problem_jobs.length === 0 ? (
        <p className="muted">No failing jobs. Start the worker with OPSDESK_JOB_FAILURE_RATE=0.5 to watch retries happen.</p>
      ) : (
        <table className="jobs card">
          <thead><tr><th>ID</th><th>Kind</th><th>Status</th><th>Attempts</th><th>Next run</th><th>Last error</th><th /></tr></thead>
          <tbody>
            {data.problem_jobs.map((j) => (
              <tr key={j.id}>
                <td className="mono">{j.id}</td>
                <td>{j.kind}</td>
                <td><span className={`job-status job-${j.status}`}>{j.status === "pending" ? "retrying" : j.status}</span></td>
                <td>{j.attempts}/{j.max_attempts}</td>
                <td>{j.status === "pending" ? <TimeAgo iso={j.run_at} /> : "—"}</td>
                <td className="err">{(j.last_error || "").split("\n")[0]}</td>
                <td>{j.status === "dead" && <button className="btn btn-sm" onClick={() => retry(j.id)}>Retry</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
