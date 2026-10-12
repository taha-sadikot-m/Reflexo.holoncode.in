// Small shared pieces for the workspace pages: page header, status badges, money, relative times, JSON.
import type { ReactNode } from "react";

import { Badge } from "./ui";

type Tone = "neutral" | "good" | "bad" | "warn" | "info" | "accent";

const STATUS_TONE: Record<string, Tone> = {
  // orders / payments
  draft: "neutral",
  awaiting_payment: "warn",
  confirmed: "good",
  preparing: "info",
  out_for_delivery: "info",
  delivered: "good",
  cancelled: "bad",
  unpaid: "warn",
  pending: "warn",
  paid: "good",
  cod_pending: "warn",
  cod_collected: "good",
  refunded: "accent",
  failed: "bad",
  created: "info",
  expired: "neutral",
  // automations / runs / steps
  active: "good",
  paused: "warn",
  archived: "neutral",
  running: "info",
  waiting: "warn",
  completed: "good",
  done: "good",
  skipped: "neutral",
  shadow: "neutral",
  // owner
  open: "warn",
  asked: "warn",
  answered: "info",
  relayed: "good",
  approved: "good",
  rejected: "bad",
  sent: "good",
  suppressed: "neutral",
  // change sets / imports / calls
  applied: "good",
  reverted: "neutral",
  queued: "neutral",
  extracting: "info",
  review: "warn",
  committed: "good",
  ringing: "info",
  connecting: "info",
  ended: "neutral",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return null;
  return <Badge tone={STATUS_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Badge>;
}

export function PageHeader({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="font-display text-xl font-bold">{title}</h1>
        {children && <p className="max-w-3xl text-sm text-dusk dark:text-stone">{children}</p>}
      </div>
      {actions}
    </div>
  );
}

export const inr = (rupees: number | null | undefined) =>
  rupees == null ? "—" : `₹${rupees.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (Math.abs(s) < 60) return s >= 0 ? "just now" : "in <1 min";
  const m = Math.round(s / 60);
  if (Math.abs(m) < 60) return m >= 0 ? `${m} min ago` : `in ${-m} min`;
  const h = Math.round(m / 60);
  if (Math.abs(h) < 24) return h >= 0 ? `${h} h ago` : `in ${-h} h`;
  return d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function fmtDate(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : "—";
}

export function Json({ value, className }: { value: unknown; className?: string }) {
  return (
    <pre className={`max-h-80 overflow-auto rounded-lg bg-bone p-2.5 font-mono text-[11px] leading-relaxed dark:bg-ink ${className ?? ""}`}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-bold uppercase tracking-wide text-stone">{label}</div>
      <div className="break-words text-sm">{children ?? "—"}</div>
    </div>
  );
}
