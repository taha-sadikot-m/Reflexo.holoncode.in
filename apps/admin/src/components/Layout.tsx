import {
  Activity,
  BookOpenCheck,
  Bomb,
  Building2,
  ChevronDown,
  Clock3,
  ExternalLink,
  FileUp,
  FlaskConical,
  Gauge,
  History,
  LayoutDashboard,
  LogOut,
  MessagesSquare,
  Phone,
  Receipt,
  RotateCcw,
  Settings,
  UserRound,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { Component, useEffect, type ErrorInfo, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useSession } from "../lib/session";
import { Button, cx } from "./ui";

type Item = { to: string; label: string; icon: LucideIcon; end?: boolean };

const SECTIONS: { title: string; items: Item[] }[] = [
  {
    title: "Operate",
    items: [
      { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
      { to: "/conversations", label: "Conversations", icon: MessagesSquare },
      { to: "/orders", label: "Orders", icon: Receipt },
      { to: "/customers", label: "Customers", icon: Users },
      { to: "/calls", label: "Calls", icon: Phone },
    ],
  },
  {
    title: "Shop brain",
    items: [
      { to: "/playbook", label: "Playbook", icon: BookOpenCheck },
      { to: "/automations", label: "Automations", icon: Workflow },
      { to: "/owner", label: "Owner inbox", icon: UserRound },
      { to: "/history", label: "Knowledge history", icon: History },
      { to: "/imports", label: "Imports", icon: FileUp },
    ],
  },
  {
    title: "Testing",
    items: [
      { to: "/lab", label: "Simulation Lab", icon: FlaskConical },
      { to: "/xray", label: "Pipeline X-ray", icon: Activity },
      { to: "/chaos", label: "Chaos", icon: Bomb },
      { to: "/evals", label: "Evals", icon: Gauge },
    ],
  },
  { title: "System", items: [{ to: "/settings", label: "Settings", icon: Settings }] },
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

function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-signal to-[#8f6bff] text-[15px] font-bold text-white shadow-sm">
        R
      </div>
      <div className="leading-tight">
        <div className="text-[15px] font-semibold tracking-tight text-ink">Reflexo</div>
        <div className="text-[11px] text-stone">Admin console</div>
      </div>
    </div>
  );
}

export function Layout() {
  const { me, businesses, business, setBusinessId, logout } = useSession();
  const location = useLocation();
  const scale = me?.demo_time_scale ?? 1;
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
          <Logo />
        </div>
        <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-4">
          {SECTIONS.map((section) => (
            <div key={section.title}>
              <div className="px-2.5 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-stone">{section.title}</div>
              <div className="space-y-0.5">
                {section.items.map(({ to, label, icon: Icon, end }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    className={({ isActive }) =>
                      cx(
                        "group flex items-center gap-2.5 rounded-md px-2.5 py-[7px] text-[13.5px] font-medium transition-colors",
                        isActive ? "bg-signal-l text-signal-d" : "text-dusk hover:bg-bone hover:text-ink",
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        <Icon size={16} strokeWidth={1.9} className={isActive ? "text-signal" : "text-stone group-hover:text-dusk"} />
                        {label}
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
              <div className="text-[11px] uppercase tracking-wide text-stone">{me?.env}</div>
            </div>
            <button onClick={() => void logout()} className="rounded-md p-1.5 text-stone hover:bg-bone hover:text-ink" title="Sign out">
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-sand bg-white px-6">
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
          <div className="flex items-center gap-2">
            {scale < 1 && (
              <span
                className="mr-1 inline-flex items-center gap-1.5 rounded-full bg-blossom px-2.5 py-1 text-[12px] font-medium text-saffron ring-1 ring-inset ring-[#fde3b8]"
                title="Waits in automations run on demo time"
              >
                <Clock3 size={13} /> Demo time · 1 day = {Math.round(86400 * scale)} s
              </span>
            )}
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
