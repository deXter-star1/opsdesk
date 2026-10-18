import { useEffect } from "react";
import { Layout } from "./components/Layout.jsx";
import { ErrorBox, Spinner } from "./components/ui.jsx";
import { api, on } from "./lib/api.js";
import { useQuery } from "./lib/hooks.js";
import { cache } from "./lib/queryCache.js";
import { startRealtime, stopRealtime } from "./lib/realtime.js";
import { matchPath, useLocation } from "./lib/router.js";
import { toast } from "./lib/toasts.js";
import { AdminJobsPage } from "./pages/AdminJobs.jsx";
import { DashboardPage } from "./pages/Dashboard.jsx";
import { ItemDetailPage } from "./pages/ItemDetail.jsx";
import { ItemsPage } from "./pages/Items.jsx";
import { LoginPage } from "./pages/Login.jsx";
import { NewItemPage } from "./pages/NewItem.jsx";
import { NotificationsPage } from "./pages/Notifications.jsx";

function Routes({ me }) {
  const { pathname } = useLocation();
  let m;
  if (pathname === "/") return <DashboardPage me={me} />;
  if (pathname === "/items") return <ItemsPage me={me} />;
  if (pathname === "/items/new") return <NewItemPage me={me} />;
  if ((m = matchPath("/items/:id", pathname))) return <ItemDetailPage key={m.id} id={m.id} me={me} />;
  if (pathname === "/notifications") return <NotificationsPage me={me} />;
  if (pathname === "/admin/jobs" && me.is_admin) return <AdminJobsPage />;
  return <div className="page"><h1>Not found</h1></div>;
}

export function App() {
  const { data: me, error, loading, refetch } = useQuery(["me"], () => api("/me"));

  useEffect(() => on("unauthorized", () => {
    stopRealtime();
    cache.setData(["me"], null);
  }), []);
  useEffect(() => on("replayed", () => {
    toast("A response was lost and the request was retried. The server recognised it, so nothing was done twice.", { kind: "info", timeout: 6000 });
  }), []);
  useEffect(() => {
    if (me) return startRealtime();
    return undefined;
  }, [me?.id]);

  if (loading && me === undefined) return <div className="boot"><Spinner /></div>;
  if (error && error.status !== 401) return <div className="boot"><ErrorBox error={error} onRetry={refetch} /></div>;
  if (!me) return <LoginPage />;
  return (
    <Layout me={me}>
      <Routes me={me} />
    </Layout>
  );
}
