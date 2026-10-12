import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import "./index.css";
import { Layout } from "./components/Layout";
import { ApiError } from "./lib/api";
import { SessionProvider, useSession } from "./lib/session";
import { Chaos } from "./pages/Chaos";
import { Conversations } from "./pages/Conversations";
import { Automations } from "./pages/Automations";
import { Calls } from "./pages/Calls";
import { Customers } from "./pages/Customers";
import { History } from "./pages/History";
import { Imports } from "./pages/Imports";
import { Lab } from "./pages/Lab";
import { Orders } from "./pages/Orders";
import { Owner } from "./pages/Owner";
import { Playbook } from "./pages/Playbook";
import { Settings } from "./pages/Settings";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Xray } from "./pages/Xray";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && [401, 403, 404].includes(error.status)) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

function App() {
  const { me, loading } = useSession();
  if (loading) return null;
  if (!me) return <Login />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="xray" element={<Xray />} />
        <Route path="conversations" element={<Conversations />} />
        <Route path="chaos" element={<Chaos />} />
        <Route path="lab" element={<Lab />} />
        <Route path="customers" element={<Customers />} />
        <Route path="orders" element={<Orders />} />
        <Route path="playbook" element={<Playbook />} />
        <Route path="automations" element={<Automations />} />
        <Route path="owner" element={<Owner />} />
        <Route path="history" element={<History />} />
        <Route path="imports" element={<Imports />} />
        <Route path="calls" element={<Calls />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename="/admin">
        <SessionProvider>
          <App />
        </SessionProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
