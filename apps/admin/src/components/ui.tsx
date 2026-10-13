import { AlertCircle, Inbox, type LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export const cx = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(" ");

/** A white surface on the canvas: optional header (title + actions), padded body. */
export function Card({
  title,
  actions,
  children,
  className,
  flush,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <section className={cx("rounded-xl bg-white shadow-card", className)}>
      {(title || actions) && (
        <header className="flex min-h-12 items-center justify-between gap-3 border-b border-sand px-5 py-3">
          <h2 className="text-[13px] font-semibold tracking-tight text-ink">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className={flush ? "" : "p-5"}>{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "bad" | "warn" | "info" | "accent";
const TONES: Record<Tone, { pill: string; dot: string }> = {
  neutral: { pill: "bg-bone text-dusk ring-sand", dot: "bg-stone" },
  good: { pill: "bg-verdant-l text-[#05603a] ring-[#c6ecdb]", dot: "bg-verdant" },
  bad: { pill: "bg-ember-l text-[#a41034] ring-[#f8cdd6]", dot: "bg-ember" },
  warn: { pill: "bg-sun-l text-[#8a4b00] ring-[#f6e1ae]", dot: "bg-sun" },
  info: { pill: "bg-sky-l text-[#0450a8] ring-[#c5dcf8]", dot: "bg-sky" },
  accent: { pill: "bg-signal-l text-signal-d ring-[#d9d6ff]", dot: "bg-signal" },
};

export function Badge({
  tone = "neutral",
  children,
  title,
  dot,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
  dot?: boolean;
}) {
  const t = TONES[tone];
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11.5px] font-medium ring-1 ring-inset",
        t.pill,
      )}
    >
      {dot && <span className={cx("h-1.5 w-1.5 rounded-full", t.dot)} />}
      {children}
    </span>
  );
}

type Variant = "primary" | "ghost" | "danger" | "subtle";

export function Button({
  variant = "primary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const styles: Record<Variant, string> = {
    primary: "bg-signal text-white shadow-sm hover:bg-signal-d",
    ghost: "bg-white text-soot shadow-sm ring-1 ring-inset ring-sand hover:bg-bone",
    danger: "bg-ember text-white shadow-sm hover:bg-[#c3153a]",
    subtle: "text-dusk hover:bg-bone hover:text-ink",
  };
  return (
    <button
      {...props}
      className={cx(
        "inline-flex h-8 items-center justify-center gap-1.5 rounded-md px-3 text-[13px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        styles[variant],
        className,
      )}
    />
  );
}

export const Mono = ({ children, className }: { children: ReactNode; className?: string }) => (
  <span className={cx("font-mono text-[11.5px] text-dusk", className)}>{children}</span>
);

/** An empty state: an icon, what is missing, and (optionally) how it gets filled. */
export function Empty({ children, icon: Icon = Inbox, hint }: { children: ReactNode; icon?: LucideIcon; hint?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-bone text-stone">
        <Icon size={18} strokeWidth={1.75} />
      </div>
      <div className="text-[13px] font-medium text-dusk">{children}</div>
      {hint && <div className="mt-1 max-w-sm text-xs text-stone">{hint}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="flex items-start gap-2 rounded-lg border border-[#f8cdd6] bg-ember-l px-3 py-2.5 text-[13px] text-[#a41034]">
      <AlertCircle size={16} className="mt-px shrink-0" />
      <span>{error instanceof Error ? error.message : String(error)}</span>
    </div>
  );
}

/** Loading placeholder: `lines` shimmering bars (or a single block with `h`). */
export function Skeleton({ lines = 3, h, className }: { lines?: number; h?: number; className?: string }) {
  if (h) return <div className={cx("skeleton rounded-lg", className)} style={{ height: h }} />;
  return (
    <div className={cx("space-y-2.5", className)}>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton h-3.5 rounded" style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

/** A number that matters, with its label and an optional hint line. */
export function Stat({
  label,
  value,
  hint,
  icon: Icon,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: LucideIcon;
  tone?: "good" | "bad" | "warn" | "accent";
}) {
  const iconTone = {
    good: "bg-verdant-l text-verdant",
    bad: "bg-ember-l text-ember",
    warn: "bg-sun-l text-sun",
    accent: "bg-signal-l text-signal",
  }[tone ?? "accent"];
  return (
    <div className="rounded-xl bg-white p-4 shadow-card">
      <div className="flex items-center justify-between">
        <span className="text-[12.5px] font-medium text-dusk">{label}</span>
        {Icon && (
          <span className={cx("flex h-7 w-7 items-center justify-center rounded-md", iconTone)}>
            <Icon size={15} strokeWidth={2} />
          </span>
        )}
      </div>
      <div className="tabular mt-2 text-[26px] font-semibold leading-none tracking-tight text-ink">{value}</div>
      {hint && <div className="mt-2 text-xs text-stone">{hint}</div>}
    </div>
  );
}

/** Segmented filter pills. */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex flex-wrap gap-1 rounded-lg bg-white p-1 shadow-card">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-md px-2.5 py-1 text-[12.5px] font-medium transition-colors",
            value === o.value ? "bg-signal-l text-signal-d" : "text-dusk hover:bg-bone hover:text-ink",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Table building blocks with consistent type and spacing. */
export const th = "px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wide text-stone";
export const td = "px-4 py-3 align-middle";
export const tr = "border-t border-sand transition-colors hover:bg-bone/70";
