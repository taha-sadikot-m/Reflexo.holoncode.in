// Small shared pieces for the workspace pages: page header, status badges, money, relative times, JSON.
import type { ReactNode } from "react";

import { useInSection } from "./section";
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
  const label = status.replace(/_/g, " ");
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"} dot>
      {label.charAt(0).toUpperCase() + label.slice(1)}
    </Badge>
  );
}

export function PageHeader({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  // inside a section (Brain, Quality) the section owns the title: the page keeps only its one-line purpose
  if (useInSection())
    return children || actions ? (
      <div className="flex flex-wrap items-center justify-between gap-3">
        {children ? <p className="max-w-3xl text-[13px] leading-relaxed text-dusk">{children}</p> : <span />}
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
    ) : null;
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">{title}</h1>
        {children && <p className="mt-1 max-w-3xl text-[13.5px] leading-relaxed text-dusk">{children}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export const inr = (rupees: number | null | undefined) =>
  rupees == null ? "-" : `₹${rupees.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "-";
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
  return iso ? new Date(iso).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : "-";
}

export function Json({ value, className }: { value: unknown; className?: string }) {
  return (
    <pre className={`max-h-80 overflow-auto rounded-lg border border-sand bg-parchment p-3 font-mono text-[11.5px] leading-relaxed text-soot ${className ?? ""}`}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-wide text-stone">{label}</div>
      <div className="mt-0.5 break-words text-[13.5px] text-ink">{children ?? "-"}</div>
    </div>
  );
}

const PAYMENT_METHOD: Record<string, string> = { razorpay_link: "Online", cod: "Cash on delivery" };
export const paymentLabel = (m: string | null | undefined) => (m ? (PAYMENT_METHOD[m] ?? m.replace(/_/g, " ")) : "-");

/** "order.cancelled" → "Order cancelled", "payment.link_created" → "Payment link created". */
export function eventLabel(type: string): string {
  const s = type.replace(/[._]/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** A small uppercase section heading inside cards and detail panes. */
export function SectionLabel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={`mb-2 text-[11px] font-semibold uppercase tracking-wide text-stone ${className ?? ""}`}>{children}</div>;
}
