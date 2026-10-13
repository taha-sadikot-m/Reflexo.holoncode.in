// The Reflexo mark and wordmark ("Reflexo." with the marigold dot), for light and dark backgrounds.
import { cx } from "./ui";

export function LogoMark({ size = 32, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className={className} aria-hidden>
      <defs>
        <linearGradient id="rx-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#7c5cff" />
          <stop offset="1" stopColor="#5236e0" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#rx-g)" />
      <path d="M10.5 23V9.5h6.2c2.9 0 4.8 1.7 4.8 4.2 0 1.9-1.1 3.3-2.8 3.9l3.2 5.4h-3.3l-2.8-5h-2.4v5h-2.9Zm2.9-7.5h3c1.3 0 2.1-.7 2.1-1.8s-.8-1.8-2.1-1.8h-3v3.6Z" fill="#fff" />
      <circle cx="24.6" cy="22.2" r="2.3" fill="#ff9e1b" />
    </svg>
  );
}

export function Logo({ dark, className, sub }: { dark?: boolean; className?: string; sub?: string }) {
  return (
    <div className={cx("flex items-center gap-2.5", className)}>
      <LogoMark />
      <div className="leading-tight">
        <div className={cx("font-display text-[17px] font-bold tracking-tight", dark ? "text-white" : "text-ink")}>
          Reflexo<span className="text-marigold">.</span>
        </div>
        {sub && <div className={cx("text-[11px]", dark ? "text-white/60" : "text-stone")}>{sub}</div>}
      </div>
    </div>
  );
}
