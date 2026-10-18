import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import "./index.css";
import { Layout } from "./components/Layout";
import { ApiError, SURFACE } from "./lib/api";
import { SessionProvider, useSession } from "./lib/session";
import { BrainSection, QualitySection } from "./pages/Sections";
import { AccessLogs } from "./pages/AccessLogs";
import { Automations } from "./pages/Automations";
import { Benchmarks } from "./pages/Benchmarks";
import { Chaos } from "./pages/Chaos";
import { Costs } from "./pages/Costs";
import { Inbox } from "./pages/Inbox";
import { Evals } from "./pages/Evals";
import { History } from "./pages/History";
import { Knowledge } from "./pages/Knowledge";
import { Lab } from "./pages/Lab";
import { Learning } from "./pages/Learning";
import { Login } from "./pages/Login";
import { Orders } from "./pages/Orders";
import { Overview } from "./pages/Overview";
import { Playbook } from "./pages/Playbook";
import { Settings } from "./pages/Settings";
import { Setup } from "./pages/Setup";
import { Xray } from "./pages/Xray";
import { Landing } from "./site/Landing";
import { Login as OwnerLogin, Signup } from "./site/Account";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && [401, 403, 404].includes(error.status)) && count < 2,
      // data stays live without a page reload: refetch when the tab regains focus or the network returns
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
      staleTime: 2_000,
    },
  },
});

/** Old addresses keep working (links in messages, bookmarks): same query string, new home. */
function Moved({ to, extra }: { to: string; extra?: string }) {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  if (extra) for (const [k, v] of new URLSearchParams(extra)) if (!params.has(k)) params.set(k, v);
  const q = params.toString();
  return <Navigate to={`${to}${q ? `?${q}` : ""}`} replace />;
}

function App() {
  const { me, loading } = useSession();
  if (loading) return null;
  const operator = me?.role !== "owner"; // the engineering pages are the operator's
  if (!me) {
    // the owner's app has no operator login: an owner signs in on the site
    if (SURFACE === "app") {
      window.location.replace("/login");
      return null;
    }
    return <Login />;
  }
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="inbox" element={<Inbox />} />
        <Route path="orders" element={<Orders />} />
        {!operator && <Route path="setup" element={<Setup />} />}
        <Route path="brain" element={<BrainSection />}>
          <Route index element={<Navigate to="learning" replace />} />
          <Route path="learning" element={<Learning />} />
          <Route path="rules" element={<Playbook />} />
          <Route path="procedures" element={<Automations />} />
          <Route path="knowledge" element={<Knowledge />} />
          <Route path="history" element={<History />} />
        </Route>
        {operator && (
          <>
            <Route path="quality" element={<QualitySection />}>
              <Route index element={<Navigate to="lab" replace />} />
              <Route path="lab" element={<Lab />} />
              <Route path="xray" element={<Xray />} />
              <Route path="chaos" element={<Chaos />} />
              <Route path="evals" element={<Evals />} />
              <Route path="costs" element={<Costs />} />
              <Route path="benchmarks" element={<Benchmarks />} />
            </Route>
            <Route path="settings" element={<Settings />} />
            <Route path="settings/access" element={<AccessLogs />} />
          </>
        )}

        {/* earlier addresses */}
        <Route path="conversations" element={<Moved to="/inbox" />} />
        <Route path="customers" element={<Moved to="/inbox" />} />
        <Route path="calls" element={<Moved to="/inbox" />} />
        <Route path="playbook" element={<Moved to="/brain/rules" />} />
        <Route path="automations" element={<Moved to="/brain/procedures" />} />
        <Route path="owner" element={<Moved to="/brain/learning" />} />
        <Route path="history" element={<Moved to="/brain/history" />} />
        <Route path="imports" element={<Moved to="/brain/history" extra="view=imports" />} />
        <Route path="lab" element={<Moved to="/quality/lab" />} />
        <Route path="xray" element={<Moved to="/quality/xray" />} />
        <Route path="chaos" element={<Moved to="/quality/chaos" />} />
        <Route path="evals" element={<Moved to="/quality/evals" />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

// the live demo carries the voice client (LiveKit): loaded only when a visitor opens it
const Demo = lazy(() => import("./site/Demo").then((m) => ({ default: m.Demo })));

/** The public site: what a visitor sees before they have an account. */
function Site() {
  return (
    <Routes>
      <Route index element={<Landing />} />
      <Route
        path="demo"
        element={
          <Suspense fallback={null}>
            <Demo />
          </Suspense>
        }
      />
      <Route path="signup" element={<Signup />} />
      <Route path="login" element={<OwnerLogin />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

// One build, three modes by the first path segment: the ops console (/admin), the owner's app (/app) and the public site.
const segment = window.location.pathname.split("/")[1];
const mode = segment === "admin" || segment === "app" ? segment : "site";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {mode === "site" ? (
        <BrowserRouter>
          <Site />
        </BrowserRouter>
      ) : (
        <BrowserRouter basename={`/${mode}`}>
          <SessionProvider>
            <App />
          </SessionProvider>
        </BrowserRouter>
      )}
    </QueryClientProvider>
  </StrictMode>,
);
