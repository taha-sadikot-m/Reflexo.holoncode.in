import { ArrowLeft } from "lucide-react";

import { Logo } from "../components/brand";

const COPY = {
  demo: ["The live demo is getting ready", "Chat with the AI employee, call it from your browser and watch the owner's side — opening shortly."],
  signup: ["Sign-up opens shortly", "Create your own AI employee from a menu photo in five minutes."],
  login: ["Owner log-in opens shortly", "Your shop's dashboard: orders, chats, calls and everything it learned."],
} as const;

/** A branded holding page for site routes that are still being opened up. */
export function SoonPage({ what }: { what: keyof typeof COPY }) {
  const [title, text] = COPY[what];
  return (
    <div className="bg-hero relative flex min-h-full flex-col text-white">
      <div className="grid-fade pointer-events-none absolute inset-0" />
      <header className="relative mx-auto flex h-16 w-full max-w-6xl items-center px-5">
        <a href="/">
          <Logo dark />
        </a>
      </header>
      <main className="relative flex flex-1 items-center justify-center px-5 pb-24">
        <div className="animate-rise max-w-lg text-center">
          <h1 className="font-display text-[36px] font-bold leading-tight tracking-tight">{title}</h1>
          <p className="mt-4 text-[16px] text-white/70">{text}</p>
          <a href="/" className="mt-8 inline-flex items-center gap-2 rounded-xl px-5 py-3 text-[14px] font-semibold ring-1 ring-white/25 hover:bg-white/10">
            <ArrowLeft size={16} /> Back to home
          </a>
        </div>
      </main>
    </div>
  );
}
