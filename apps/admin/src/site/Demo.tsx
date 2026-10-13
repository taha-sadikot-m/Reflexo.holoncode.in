// The public live demo: a private copy of Meera's Bakes per visitor. The visitor is the customer (chat and voice
// call) and the owner (the phone the AI asks and learns on), and can open the owner's dashboard for the same shop.
// Everything here is the real product running on simulated phones — nothing is scripted.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  BookOpenCheck,
  Brain,
  Check,
  Gauge,
  LayoutDashboard,
  Loader2,
  MessageCircle,
  Phone,
  RotateCcw,
  ShieldCheck,
  ShoppingBag,
  Sparkles,
  UserRound,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { Logo } from "../components/brand";
import { CallScreen, useWebCall } from "../components/call";
import { Device } from "../components/phone";
import { cx } from "../components/ui";
import { api, ApiError, setCsrf, uploadSimMedia, type Business, type Me, type SimMediaRef, type SimThread, type TraceSummary } from "../lib/api";
import { traceLabel } from "../lib/stages";

const ACTOR = 1;
const NAME_KEY = "reflexo.demo.name";

function readName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? "";
  } catch {
    return "";
  }
}

async function startDemo(): Promise<{ csrf: string }> {
  const resp = await fetch("/api/demo/start", { method: "POST", credentials: "same-origin", headers: { Accept: "application/json" } });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(resp.status, body.code ?? "ERROR", body.detail ?? "The demo could not start. Please try again.");
  return body;
}

function useDemoSession() {
  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api.get<Me>("/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
  useEffect(() => {
    if (me.data) setCsrf(me.data.csrf);
  }, [me.data]);
  const shops = useQuery({ queryKey: ["businesses"], queryFn: () => api.get<Business[]>("/businesses"), enabled: !!me.data });
  // a demo session is an owner session on a demo shop; a real owner signed in here gets a fresh demo instead
  const shop = (shops.data ?? []).find((b) => b.slug.startsWith("demo-")) ?? null;
  return { loading: me.isLoading || (!!me.data && shops.isLoading), shop };
}

// ---------------------------------------------------------------- intro (before the shop exists)

function Intro({ onStart, starting, error }: { onStart: (name: string) => void; starting: boolean; error: unknown }) {
  const [name, setName] = useState(readName);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onStart(name.trim());
  };
  const points: [LucideIcon, string, string][] = [
    [MessageCircle, "Be the customer", "Chat on a WhatsApp-style phone — order, pay, change your mind."],
    [Phone, "Call the shop", "Talk to the AI from your browser in Hindi, English or Hinglish."],
    [Brain, "Be the owner", "See what it asks you, decide with one tap, and watch it learn."],
    [LayoutDashboard, "Open the dashboard", "Orders, chats, calls and everything it learned — your shop's back office."],
  ];
  return (
    <div className="bg-hero relative min-h-full overflow-hidden text-white">
      <div className="grid-fade pointer-events-none absolute inset-0" />
      <header className="relative mx-auto flex h-16 max-w-6xl items-center justify-between px-5">
        <a href="/">
          <Logo dark />
        </a>
        <a href="/" className="text-[14px] text-white/70 hover:text-white">
          Back to home
        </a>
      </header>
      <main className="relative mx-auto grid max-w-6xl items-center gap-12 px-5 pb-20 pt-10 lg:grid-cols-[1.1fr_1fr]">
        <div className="animate-rise">
          <span className="inline-flex items-center gap-2 rounded-full bg-white/10 px-3 py-1 text-[12.5px] font-medium text-white/85 ring-1 ring-white/15">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[#25d366] opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-[#25d366]" />
            </span>
            Live — the real product, not a recording
          </span>
          <h1 className="mt-6 font-display text-[40px] font-bold leading-[1.08] tracking-tight sm:text-[52px]">
            Meet Meera's
            <br />
            <span className="text-gradient">AI employee.</span>
          </h1>
          <p className="mt-5 max-w-xl text-[16.5px] leading-relaxed text-white/75">
            Meera runs a home bakery in Pune. You get your own private copy of her shop — her menu, prices and policies — with an AI employee that is
            ready to work. Play both sides.
          </p>
          <form onSubmit={submit} className="mt-8 flex max-w-lg flex-col gap-3 sm:flex-row">
            <label className="relative flex-1">
              <UserRound size={17} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-white/50" />
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={40}
                placeholder="Your first name (as the customer)"
                className="h-12 w-full rounded-xl bg-white/10 pl-10 pr-4 text-[15px] text-white outline-none ring-1 ring-white/20 placeholder:text-white/45 focus:ring-2 focus:ring-marigold"
              />
            </label>
            <button
              type="submit"
              disabled={starting}
              className="inline-flex h-12 items-center justify-center gap-2 rounded-xl bg-marigold px-6 text-[15px] font-semibold text-night shadow-[0_10px_30px_-10px_rgba(255,158,27,0.7)] transition hover:-translate-y-0.5 hover:bg-[#ffab3a] disabled:opacity-70"
            >
              {starting ? <Loader2 size={18} className="animate-spin" /> : <Sparkles size={18} />}
              {starting ? "Opening your shop…" : "Start the live demo"}
            </button>
          </form>
          {error ? <div className="mt-3 text-[13.5px] text-[#ffc9bd]">{error instanceof Error ? error.message : "Something went wrong."}</div> : null}
          <div className="mt-4 text-[12.5px] text-white/50">No sign-up. Your shop is private to you and is deleted after 24 hours. Payments are in test mode.</div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {points.map(([Icon, title, text], i) => (
            <div key={title} className="animate-rise rounded-2xl bg-white/[0.07] p-5 ring-1 ring-white/10 backdrop-blur" style={{ animationDelay: `${i * 80}ms` }}>
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-white/10 text-marigold">
                <Icon size={19} />
              </span>
              <div className="mt-4 font-display text-[16px] font-semibold">{title}</div>
              <div className="mt-1 text-[13.5px] leading-relaxed text-white/65">{text}</div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------- the guide

type Try = { who: "customer" | "owner" | "call" | "link"; text: string; send?: string; href?: string };
type Mission = { icon: LucideIcon; title: string; what: string; tries: Try[] };

const MISSIONS: Mission[] = [
  {
    icon: MessageCircle,
    title: "Ask like a real customer",
    what: "Answers come from Meera's own menu and policies — never invented.",
    tries: [
      { who: "customer", text: "Eggless cakes milte hain? Price?", send: "Eggless cakes milte hain? Price kya hai?" },
      { who: "customer", text: "Do you deliver to Baner?", send: "Do you deliver to Baner? What is the delivery charge?" },
    ],
  },
  {
    icon: ShoppingBag,
    title: "Order and pay",
    what: "It collects every detail, confirms, sends a payment link and confirms when paid.",
    tries: [
      { who: "customer", text: "1 kg chocolate truffle, kal shaam", send: "1 kg chocolate truffle cake chahiye kal shaam 6 baje, Kothrud delivery" },
      { who: "customer", text: "Then tap the buttons and “Pay (test)”" },
    ],
  },
  {
    icon: Phone,
    title: "Call the shop",
    what: "Tap the phone icon on the customer's chat and talk. Mid-call, send a cake photo with 📎 — it finds the match while you talk.",
    tries: [{ who: "call", text: "Start a call" }],
  },
  {
    icon: Brain,
    title: "Watch it ask the owner — and learn",
    what: "After paying, ask to cancel. A paid order is Meera's call and she never wrote a rule for it, so the AI asks her phone. Decide, then tap “Always” — next time it knows.",
    tries: [{ who: "customer", text: "Cancel my order", send: "Mera order cancel kar do please, plans change ho gaye" }],
  },
  {
    icon: BookOpenCheck,
    title: "Be the owner",
    what: "Change the business in plain words. It reads back what it understood, and /undo reverts it.",
    tries: [
      { who: "owner", text: "1 kg truffle is now ₹999", send: "Chocolate truffle 1 kg ka price ab 999 kar do" },
      { who: "owner", text: "/undo", send: "/undo" },
    ],
  },
  {
    icon: LayoutDashboard,
    title: "Open the owner's dashboard",
    what: "The back office for this same shop: orders, every chat and call, and the Brain with what it learned.",
    tries: [{ who: "link", text: "Open the dashboard", href: "/app" }],
  },
];

function Guide({
  onTry,
  done,
}: {
  onTry: (t: Try, key: string) => void;
  done: Set<string>;
}) {
  return (
    <div className="space-y-2.5">
      {MISSIONS.map((m, i) => {
        const finished = m.tries.some((_, j) => done.has(`${i}.${j}`));
        return (
          <div key={m.title} className={cx("rounded-2xl border bg-white p-4 transition", finished ? "border-verdant/30" : "border-sand")}>
            <div className="flex items-start gap-3">
              <span className={cx("mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg", finished ? "bg-verdant-l text-verdant" : "bg-signal-l text-signal")}>
                {finished ? <Check size={16} /> : <m.icon size={16} />}
              </span>
              <div className="min-w-0">
                <div className="text-[14px] font-semibold text-ink">
                  <span className="text-stone">{i + 1}.</span> {m.title}
                </div>
                <div className="mt-0.5 text-[12.5px] leading-snug text-dusk">{m.what}</div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {m.tries.map((t, j) =>
                    t.send || t.who === "call" || t.href ? (
                      <button
                        key={t.text}
                        onClick={() => onTry(t, `${i}.${j}`)}
                        className={cx(
                          "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-medium ring-1 transition hover:-translate-y-px",
                          t.who === "owner" ? "bg-blossom text-marigold-d ring-[#ffd9a3]" : "bg-signal-l text-signal-d ring-[#ddd5ff]",
                        )}
                      >
                        {t.who === "owner" ? "Owner:" : t.who === "customer" ? "Say:" : ""} {t.text}
                        <ArrowRight size={12} />
                      </button>
                    ) : (
                      <span key={t.text} className="text-[12px] text-stone">
                        {t.text}
                      </span>
                    ),
                  )}
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- under the hood

function UnderTheHood({ businessId }: { businessId: string }) {
  const traces = useQuery({
    queryKey: ["traces", businessId, "demo"],
    queryFn: () => api.get<{ items: TraceSummary[] }>("/traces", { business_id: businessId, limit: "6" }),
    refetchInterval: 3000,
  });
  const items = (traces.data?.items ?? []).filter((t) => t.turn_id);
  return (
    <div className="rounded-2xl border border-sand bg-white p-4">
      <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
        <Gauge size={15} className="text-signal" /> Under the hood
      </div>
      <div className="mt-0.5 text-[12px] text-stone">Every message is traced: model, tools, the safety check, time to reply.</div>
      <ul className="mt-3 space-y-2">
        {items.length === 0 && <li className="text-[12.5px] text-stone">Send a message to see it work.</li>}
        {items.slice(0, 4).map((t) => (
          <li key={t.trace_id} className="animate-rise rounded-xl bg-parchment px-3 py-2">
            <div className="truncate text-[12.5px] font-medium text-ink">{traceLabel(t)}</div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-dusk">
              <span className="tabular font-mono">{(t.total_ms / 1000).toFixed(1)} s</span>
              {t.model && <span>{t.model}</span>}
              <span>{t.stages} steps</span>
              {t.gate !== null && (
                <span className={cx("inline-flex items-center gap-1", t.gate ? "text-verdant" : "text-ember")}>
                  {t.gate ? <ShieldCheck size={12} /> : <XCircle size={12} />} {t.gate ? "fact-checked" : "blocked & repaired"}
                </span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- the live demo

function Live({ shop, onRestart, restarting }: { shop: Business; onRestart: () => void; restarting: boolean }) {
  const qc = useQueryClient();
  const bid = shop.id;
  const name = readName() || "Guest";
  const [customerDraft, setCustomerDraft] = useState("");
  const [ownerDraft, setOwnerDraft] = useState("");
  const [done, setDone] = useState<Set<string>>(new Set());
  const call = useWebCall(bid, ACTOR, name);
  const customer = useQuery({
    queryKey: ["sim-thread", bid, ACTOR],
    queryFn: () => api.get<SimThread>("/sim/thread", { business_id: bid, actor: String(ACTOR) }),
    refetchInterval: 1500,
  });
  const owner = useQuery({
    queryKey: ["sim-owner", bid],
    queryFn: () => api.get<SimThread>("/sim/owner/thread", { business_id: bid }),
    refetchInterval: 1500,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["sim-thread", bid] });
    void qc.invalidateQueries({ queryKey: ["sim-owner", bid] });
  };
  type Body = { text?: string; option_id?: string; option_title?: string; media?: SimMediaRef };
  const sendCustomer = useMutation({
    mutationFn: (body: Body) => api.post("/sim/send", { actor: ACTOR, name, ...body }, { business_id: bid }),
    onSuccess: refresh,
  });
  const sendOwner = useMutation({
    mutationFn: (body: Body) => api.post("/sim/owner/send", body, { business_id: bid }),
    onSuccess: refresh,
  });
  const pay = useMutation({ mutationFn: (id: string) => api.post(`/sim/razorpay/pay/${id}`, {}, { business_id: bid }), onSuccess: refresh });

  const onTry = (t: Try, key: string) => {
    setDone((d) => new Set(d).add(key));
    if (t.who === "customer" && t.send) sendCustomer.mutate({ text: t.send });
    else if (t.who === "owner" && t.send) sendOwner.mutate({ text: t.send });
    else if (t.who === "call") void call.start();
    else if (t.href) window.open(t.href, "_blank", "noopener");
  };
  const ownerWaiting = (owner.data?.items ?? []).some((it, i, all) => it.from === "business" && it.payload?.type === "interactive" && !all.slice(i + 1).some((n) => n.from !== "business"));

  return (
    <div className="min-h-full bg-parchment">
      <header className="sticky top-0 z-20 border-b border-sand bg-white/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-[1380px] items-center justify-between gap-4 px-5">
          <div className="flex items-center gap-4">
            <a href="/">
              <Logo />
            </a>
            <span className="hidden items-center gap-1.5 rounded-full bg-verdant-l px-2.5 py-1 text-[12px] font-semibold text-[#05603a] sm:inline-flex">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-verdant" /> Live demo · your private {shop.name}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onRestart}
              disabled={restarting}
              className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] font-medium text-dusk hover:bg-bone hover:text-ink disabled:opacity-50"
              title="A fresh copy of the shop"
            >
              {restarting ? <Loader2 size={14} className="animate-spin" /> : <RotateCcw size={14} />} Start over
            </button>
            <a
              href="/app"
              target="_blank"
              rel="noopener"
              className="inline-flex items-center gap-1.5 rounded-lg bg-signal px-3.5 py-2 text-[13px] font-semibold text-white shadow-glow transition hover:bg-signal-d"
            >
              <LayoutDashboard size={15} /> Owner dashboard
            </a>
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-[1380px] gap-6 px-5 py-6 xl:grid-cols-[minmax(0,1fr)_380px_380px]">
        <aside className="order-3 space-y-4 xl:order-1">
          <div>
            <h1 className="font-display text-[22px] font-bold tracking-tight text-ink">Try it — both sides</h1>
            <p className="mt-1 text-[13.5px] text-dusk">Tap a suggestion or type your own. Everything is live: the AI, the payments (test mode) and the learning.</p>
          </div>
          <Guide onTry={onTry} done={done} />
          <UnderTheHood businessId={bid} />
        </aside>

        <Column order="order-1 xl:order-2" label="The customer — you" hint="WhatsApp, as your customers see it" tone="signal">
          <Device
            title={shop.name}
            subtitle="online · replies instantly"
            thread={customer.data}
            error={customer.error ?? sendCustomer.error ?? pay.error}
            busy={sendCustomer.isPending || pay.isPending}
            onSend={(text) => sendCustomer.mutate({ text })}
            onTap={(o) => sendCustomer.mutate({ option_id: o.id, option_title: o.title })}
            onPay={(id) => pay.mutate(id)}
            draft={customerDraft}
            onDraft={setCustomerDraft}
            onAttach={async (file, caption) => {
              const media = await uploadSimMedia(bid, file);
              await sendCustomer.mutateAsync({ media, text: caption || undefined });
            }}
            emptyHint={
              <>
                Say hi to {shop.name} — or tap a suggestion.
                <br />
                Tap <Phone size={11} className="inline" /> above to call.
              </>
            }
            actions={
              <button
                onClick={() => void call.start()}
                disabled={call.state !== "idle"}
                className="rounded-full p-2 transition hover:bg-white/15 disabled:opacity-50"
                title="Voice call"
              >
                <Phone size={18} />
              </button>
            }
            overlay={<CallScreen call={call} shop={shop.name} />}
            height="h-[620px]"
          />
        </Column>

        <Column
          order="order-2 xl:order-3"
          label="The owner — Meera's phone"
          hint={ownerWaiting ? "The AI is waiting for your decision" : "Where the AI asks, reports and learns"}
          tone="marigold"
          alert={ownerWaiting}
        >
          <Device
            title="Reflexo · Meera's assistant"
            subtitle="your AI employee"
            avatar={<Sparkles size={16} />}
            thread={owner.data}
            error={owner.error ?? sendOwner.error}
            busy={sendOwner.isPending}
            onSend={(text) => sendOwner.mutate({ text })}
            onTap={(o) => sendOwner.mutate({ option_id: o.id, option_title: o.title })}
            draft={ownerDraft}
            onDraft={setOwnerDraft}
            onAttach={async (file, caption) => {
              const media = await uploadSimMedia(bid, file);
              await sendOwner.mutateAsync({ media, text: caption || undefined });
            }}
            placeholder="Message your AI employee"
            emptyHint="When the AI isn't sure, it asks you here — with buttons. You can also tell it to change prices, timings or rules, or send a photo of a new menu."
            height="h-[620px]"
          />
        </Column>
      </main>
    </div>
  );
}

function Column({
  label,
  hint,
  tone,
  alert,
  order,
  children,
}: {
  label: string;
  hint: string;
  tone: "signal" | "marigold";
  alert?: boolean;
  order: string;
  children: ReactNode;
}) {
  return (
    <section className={cx("min-w-0", order)}>
      <div className="mb-2.5 flex items-center justify-between px-1">
        <div>
          <div className={cx("text-[12px] font-semibold uppercase tracking-wider", tone === "signal" ? "text-signal" : "text-marigold-d")}>{label}</div>
          <div className={cx("text-[12.5px]", alert ? "font-medium text-marigold-d" : "text-stone")}>{hint}</div>
        </div>
        {alert && (
          <span className="relative flex h-2.5 w-2.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-marigold opacity-75" />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-marigold" />
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

export function Demo() {
  const qc = useQueryClient();
  const { loading, shop } = useDemoSession();
  const start = useMutation({
    mutationFn: async (name?: string) => {
      if (name !== undefined) {
        try {
          localStorage.setItem(NAME_KEY, name);
        } catch {
          /* storage unavailable */
        }
      }
      const res = await startDemo();
      setCsrf(res.csrf);
      await qc.resetQueries(); // a new shop: nothing cached from the previous one may show

    },
  });
  if (loading)
    return (
      <div className="flex min-h-full items-center justify-center bg-parchment">
        <Loader2 className="animate-spin text-signal" />
      </div>
    );
  if (!shop) return <Intro onStart={(n) => start.mutate(n)} starting={start.isPending} error={start.error} />;
  return <Live shop={shop} onRestart={() => start.mutate(undefined)} restarting={start.isPending} />;
}
