import { Activity, LayoutDashboard, LogOut, MessagesSquare, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useSession } from "../lib/session";
import { cx } from "./ui";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/xray", label: "Pipeline X-ray", icon: Activity },
  { to: "/conversations", label: "Conversations", icon: MessagesSquare },
];

export function Layout() {
  const { me, businesses, business, setBusinessId, logout } = useSession();
  const [dark, setDark] = useState(() => localStorage.getItem("reflexo.admin.theme") === "dark");
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("reflexo.admin.theme", dark ? "dark" : "light");
  }, [dark]);
  const scale = me?.demo_time_scale ?? 1;

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col bg-ink text-bone">
        <div className="px-5 py-5">
          <div className="font-display text-lg font-bold">
            REFLEXO<span className="text-marigold">.</span>
          </div>
          <div className="font-mono text-[10px] tracking-wider text-stone">ADMIN · {me?.env?.toUpperCase()}</div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 px-3">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cx("flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm", isActive ? "bg-twilight text-white" : "text-stone hover:bg-indigo hover:text-bone")
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-twilight px-3 py-3 text-xs text-stone">
          <div className="mb-2 px-2">Signed in as {me?.user}</div>
          <div className="flex gap-1">
            <button onClick={() => setDark(!dark)} className="flex-1 rounded-lg px-2 py-1.5 hover:bg-indigo" title="Toggle theme">
              {dark ? <Sun size={14} className="mx-auto" /> : <Moon size={14} className="mx-auto" />}
            </button>
            <button onClick={() => void logout()} className="flex-1 rounded-lg px-2 py-1.5 hover:bg-indigo" title="Sign out">
              <LogOut size={14} className="mx-auto" />
            </button>
          </div>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        {scale < 1 && (
          <div className="bg-sun px-4 py-1 text-center text-xs font-semibold text-ink">⏱ Demo time: 1 day = {Math.round(86400 * scale)} s</div>
        )}
        <header className="flex items-center justify-between border-b border-sand bg-white px-6 py-3 dark:border-twilight dark:bg-indigo">
          <select
            aria-label="Business"
            value={business?.id ?? ""}
            onChange={(e) => setBusinessId(e.target.value)}
            className="rounded-lg border border-sand bg-parchment px-3 py-1.5 text-sm font-semibold dark:border-twilight dark:bg-ink"
          >
            {businesses.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
                {b.kill_switch ? " (paused)" : ""}
              </option>
            ))}
          </select>
          <a href={me?.links.jaeger ?? "/jaeger"} target="_blank" rel="noreferrer" className="text-xs font-semibold text-signal hover:underline">
            Jaeger ↗
          </a>
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
