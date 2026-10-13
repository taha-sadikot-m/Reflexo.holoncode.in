// A top-level area (Brain, Quality) with sub-tabs; its pages render inside it without their own big title.
import type { LucideIcon } from "lucide-react";
import { createContext, useContext, type ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { cx } from "./ui";

export const SectionContext = createContext(false);
export const useInSection = () => useContext(SectionContext);

export type SubTab = { to: string; label: string; icon?: LucideIcon; count?: number | null };

export function Section({ title, description, tabs, children }: { title: string; description?: ReactNode; tabs: SubTab[]; children?: ReactNode }) {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-[13.5px] leading-relaxed text-dusk">{description}</p>}
      </div>
      <nav className="flex gap-1 overflow-x-auto border-b border-sand">
        {tabs.map(({ to, label, icon: Icon, count }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cx(
                "-mb-px inline-flex shrink-0 items-center gap-1.5 border-b-2 px-3 pb-2.5 pt-1 text-[13.5px] font-medium transition-colors",
                isActive ? "border-signal text-signal-d" : "border-transparent text-dusk hover:border-sand hover:text-ink",
              )
            }
          >
            {Icon && <Icon size={15} strokeWidth={1.9} />}
            {label}
            {!!count && <span className="tabular rounded-full bg-sun-l px-1.5 text-[11px] font-semibold text-[#8a4b00]">{count}</span>}
          </NavLink>
        ))}
      </nav>
      <SectionContext.Provider value={true}>{children ?? <Outlet />}</SectionContext.Provider>
    </div>
  );
}
