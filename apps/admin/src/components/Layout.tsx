import {
  Brain,
  Building2,
  ChevronDown,
  Clock3,
  ExternalLink,
  Inbox,
  LayoutDashboard,
  LogOut,
  Receipt,
  RotateCcw,
  Settings,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Component, useEffect, type ErrorInfo, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api, type ConversationItem, type OwnerWorkspace } from "../lib/api";
import { useSession } from "../lib/session";
import { Logo } from "./brand";
import { Button, cx } from "./ui";

type Item = { to: string; label: string; icon: LucideIcon; end?: boolean; count?: "inbox" | "brain"; hint: string };

const SECTIONS: { title: string; items: Item[] }[] = [
  {
    title: "Business",
    items: [
      { to: "/", label: "Home", icon: LayoutDashboard, end: true, hint: "Today at a glance" },
      { to: "/inbox", label: "Inbox", icon: Inbox, count: "inbox", hint: "Chats, calls and customers" },
      { to: "/orders", label: "Orders", icon: Receipt, hint: "Orders, payments, capacity" },
      { to: "/brain", label: "Brain", icon: Brain, count: "brain", hint: "Learning, rules, knowledge" },
    ],
  },
  {
    title: "Engineering",
    items: [
      { to: "/quality", label: "Quality", icon: ShieldCheck, hint: "Lab, X-ray, chaos, evals" },
      { to: "/settings", label: "Settings", icon: Settings, hint: "Channels, reset, flags" },
    ],
  },
];

/** One page failing must never blank the panel: it shows what went wrong, the rest keeps working. */
class PageBoundary extends Component<{ children: ReactNode; resetKey: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("page error", error, info.componentStack);
  }
  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="mx-auto mt-16 max-w-md rounded-xl bg-white p-6 text-center shadow-card">
        <div className="text-sm font-semibold text-ink">This page ran into a problem</div>
        <div className="mt-1 break-words text-xs text-stone">{this.state.error.message}</div>
        <Button variant="ghost" className="mt-4" onClick={() => this.setState({ error: null })}>
          <RotateCcw size={14} /> Try again
        </Button>
      </div>
    );
  }
}

export function Layout() {
  const { me, businesses, business, setBusinessId, logout } = useSession();
  const location = useLocation();
  const operator = me?.role !== "owner";
  // an owner sees their shop; the engineering tools are the operator's
  const sections = operator ? SECTIONS : SECTIONS.slice(0, 1).map((s) => ({ ...s, title: "Your shop" }));
  const scale = me?.demo_time_scale ?? 1;
  const bid = business?.id ?? "";
  // the same queries the pages use (shared cache): how much needs a person, at a glance
  const convs = useQuery({
    queryKey: ["conversations", bid],
    queryFn: () => api.get<{ items: ConversationItem[] }>("/conversations", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 15000,
  });
  const owner = useQuery({
    queryKey: ["owner", bid],
    queryFn: () => api.get<OwnerWorkspace>("/owner", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 15000,
  });
  const counts = {
    inbox: (convs.data?.items ?? []).filter((c) => (c.needs_attention || c.human_active) && (business?.sandbox || !c.simulated)).length,
    brain:
      (owner.data?.questions ?? []).filter((q) => q.status === "open" || q.status === "asked").length +
      (owner.data?.approvals ?? []).filter((a) => a.status === "pending").length,
  };
  useEffect(() => {
    // the panel is light only; clear what an older build's dark toggle left behind
    document.documentElement.classList.remove("dark");
    try {
      localStorage.removeItem("reflexo.admin.theme");
    } catch {
      /* storage unavailable */
    }
  }, []);

  return (
    <div className="flex h-full">
      <aside className="flex w-60 shrink-0 flex-col border-r border-sand bg-white">
        <div className="px-5 pb-4 pt-5">
          <Logo sub={operator ? "Operations console" : "Your AI employee"} />
        </div>
        <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-4">
          {sections.map((section) => (
            <div key={section.title}>
              <div className="px-2.5 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-stone">{section.title}</div>
              <div className="space-y-0.5">
                {section.items.map(({ to, label, icon: Icon, end, count, hint }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    className={({ isActive }) =>
                      cx(
                        "group flex items-center gap-3 rounded-lg px-2.5 py-2 text-[13.5px] font-medium transition-colors",
                        isActive ? "bg-signal-l text-signal-d" : "text-dusk hover:bg-bone hover:text-ink",
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        <Icon size={17} strokeWidth={1.9} className={isActive ? "text-signal" : "text-stone group-hover:text-dusk"} />
                        <span className="min-w-0 flex-1">
                          <span className="block">{label}</span>
                          <span className={cx("block truncate text-[11.5px] font-normal", isActive ? "text-signal-d/70" : "text-stone")}>{hint}</span>
                        </span>
                        {count && counts[count] > 0 && (
                          <span className="tabular rounded-full bg-ember px-1.5 py-px text-[11px] font-semibold text-white">{counts[count]}</span>
                        )}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="border-t border-sand px-3 py-3">
          <div className="flex items-center gap-2.5 rounded-md px-2 py-1.5">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-bone text-xs font-semibold uppercase text-dusk">
              {me?.user?.slice(0, 1) ?? "?"}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium text-ink">{me?.user}</div>
              <div className="text-[11px] uppercase tracking-wide text-stone">{operator ? me?.env : "Owner"}</div>
            </div>
            <button onClick={() => void logout()} className="rounded-md p-1.5 text-stone hover:bg-bone hover:text-ink" title="Sign out">
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-sand bg-white px-6">
          {operator ? (
            <label className="relative flex items-center">
              <Building2 size={15} className="pointer-events-none absolute left-2.5 text-stone" />
              <select
                aria-label="Business"
                value={business?.id ?? ""}
                onChange={(e) => setBusinessId(e.target.value)}
                className="h-8 appearance-none rounded-md bg-white pl-8 pr-8 text-[13px] font-medium text-ink shadow-sm ring-1 ring-inset ring-sand hover:bg-bone focus:outline-none"
              >
                {businesses.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                    {b.kill_switch ? " (paused)" : ""}
                  </option>
                ))}
              </select>
              <ChevronDown size={14} className="pointer-events-none absolute right-2.5 text-stone" />
            </label>
          ) : (
            <div className="flex items-center gap-2 text-[14px] font-semibold text-ink">
              <Building2 size={15} className="text-stone" /> {business?.name}
              {business?.sandbox && <span className="rounded-full bg-verdant-l px-2 py-0.5 text-[11.5px] font-semibold text-[#05603a]">Live demo</span>}
            </div>
          )}
          <div className="flex items-center gap-2">
            {operator && scale < 1 && (
              <span
                className="mr-1 inline-flex items-center gap-1.5 rounded-full bg-blossom px-2.5 py-1 text-[12px] font-medium text-saffron ring-1 ring-inset ring-[#fde3b8]"
                title="Waits in automations run on demo time"
              >
                <Clock3 size={13} /> Demo time · 1 day = {Math.round(86400 * scale)} s
              </span>
            )}
            {operator ? (
              <>
                {[
                  ["Temporal", me?.links.temporal ?? "/temporal"],
                  ["Jaeger", me?.links.jaeger ?? "/jaeger"],
                ].map(([label, href]) => (
                  <a
                    key={label}
                    href={href}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12.5px] font-medium text-dusk hover:bg-bone hover:text-ink"
                  >
                    {label} <ExternalLink size={12} />
                  </a>
                ))}
              </>
            ) : (
              business?.sandbox && (
                <a href="/demo" className="inline-flex items-center gap-1.5 rounded-lg bg-signal px-3 py-1.5 text-[12.5px] font-semibold text-white hover:bg-signal-d">
                  Back to the live demo
                </a>
              )
            )}
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-auto">
          <div className="mx-auto max-w-[1440px] px-8 py-7">
            <PageBoundary resetKey={location.pathname}>
              <Outlet />
            </PageBoundary>
          </div>
        </main>
      </div>
    </div>
  );
}
