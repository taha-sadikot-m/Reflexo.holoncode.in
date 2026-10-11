import type { ButtonHTMLAttributes, ReactNode } from "react";

export const cx = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(" ");

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("rounded-xl border border-sand bg-white shadow-sm dark:border-twilight dark:bg-indigo", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between border-b border-sand px-4 py-3 dark:border-twilight">
          <h2 className="font-display text-sm font-semibold">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "bad" | "warn" | "info" | "accent";
const TONES: Record<Tone, string> = {
  neutral: "bg-bone text-dusk dark:bg-twilight dark:text-sand",
  good: "bg-verdant-l text-[#00785a] dark:bg-[#0a3b2e] dark:text-verdant",
  bad: "bg-ember-l text-ember dark:bg-[#40201c]",
  warn: "bg-sun-l text-[#8a6100] dark:bg-[#3d3210] dark:text-sun",
  info: "bg-signal-l text-signal dark:bg-[#26214d] dark:text-[#b4abff]",
  accent: "bg-blossom text-saffron dark:bg-[#3d2a10] dark:text-marigold",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold whitespace-nowrap", TONES[tone])}>
      {children}
    </span>
  );
}

export function Button({ variant = "primary", className, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" }) {
  const styles = {
    primary: "bg-ink text-white hover:bg-twilight dark:bg-marigold dark:text-ink dark:hover:bg-saffron",
    ghost: "border border-sand bg-transparent hover:bg-bone dark:border-twilight dark:hover:bg-twilight",
    danger: "bg-ember text-white hover:opacity-90",
  }[variant];
  return (
    <button
      {...props}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50",
        styles,
        className,
      )}
    />
  );
}

export const Mono = ({ children, className }: { children: ReactNode; className?: string }) => (
  <span className={cx("font-mono text-[11px] text-dusk dark:text-stone", className)}>{children}</span>
);

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-10 text-center text-sm text-stone">{children}</div>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="rounded-lg bg-ember-l px-3 py-2 text-sm text-ember">{error instanceof Error ? error.message : String(error)}</div>;
}
