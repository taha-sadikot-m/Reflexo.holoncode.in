import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api, ApiError, setCsrf, type Business, type Me } from "./api";

interface SessionValue {
  me: Me | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  businesses: Business[];
  business: Business | null;
  setBusinessId: (id: string) => void;
}

const SessionContext = createContext<SessionValue | null>(null);
const BUSINESS_KEY = "reflexo.admin.business";
const DEFAULT_SLUG = "meeras-bakes";

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const meQuery = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api.get<Me>("/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
  const me = meQuery.data ?? null;
  useEffect(() => {
    setCsrf(me?.csrf ?? null);
  }, [me]);

  const bizQuery = useQuery({
    queryKey: ["businesses"],
    queryFn: () => api.get<Business[]>("/businesses"),
    enabled: !!me,
  });
  const [businessId, setBusinessIdState] = useState<string | null>(() => localStorage.getItem(BUSINESS_KEY));
  const businesses = bizQuery.data ?? [];
  // the remembered business, else the demo business (Meera's Bakes), else the first one — never a test business
  const business =
    businesses.find((b) => b.id === businessId) ??
    businesses.find((b) => b.slug === DEFAULT_SLUG) ??
    businesses.find((b) => !b.slug.startsWith("eval")) ??
    businesses[0] ??
    null;

  const value = useMemo<SessionValue>(
    () => ({
      me,
      loading: meQuery.isLoading,
      businesses,
      business,
      setBusinessId: (id: string) => {
        localStorage.setItem(BUSINESS_KEY, id);
        setBusinessIdState(id);
        qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "me" && q.queryKey[0] !== "businesses" });
      },
      login: async (username, password) => {
        const res = await api.post<{ csrf: string }>("/login", { username, password });
        setCsrf(res.csrf);
        await qc.invalidateQueries({ queryKey: ["me"] });
      },
      logout: async () => {
        await api.post("/logout");
        setCsrf(null);
        qc.clear();
        await qc.invalidateQueries({ queryKey: ["me"] });
      },
    }),
    [me, meQuery.isLoading, businesses, business, qc],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession outside SessionProvider");
  return ctx;
}
