import {
  ArrowRight,
  BadgeCheck,
  BookOpenCheck,
  Brain,
  Camera,
  Check,
  CheckCheck,
  CreditCard,
  FileImage,
  Languages,
  MessageCircle,
  MessageSquare,
  Mic,
  Phone,
  PhoneCall,
  Send,
  ShieldCheck,
  Sparkles,
  Store,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { Logo } from "../components/brand";
import { cx } from "../components/ui";

const go = (path: string) => () => {
  window.location.href = path;
};

function PrimaryCta({ children, href, className }: { children: ReactNode; href: string; className?: string }) {
  return (
    <a
      href={href}
      className={cx(
        "inline-flex h-12 items-center gap-2 rounded-xl bg-marigold px-6 text-[15px] font-semibold text-night shadow-[0_10px_30px_-10px_rgba(255,158,27,0.7)] transition hover:-translate-y-0.5 hover:bg-[#ffab3a]",
        className,
      )}
    >
      {children}
    </a>
  );
}

function Nav() {
  return (
    <header className="absolute inset-x-0 top-0 z-20">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5">
        <Logo dark />
        <nav className="hidden items-center gap-8 text-[14px] text-white/75 md:flex">
          <a href="#how" className="hover:text-white">How it works</a>
          <a href="#features" className="hover:text-white">Features</a>
          <a href="#learning" className="hover:text-white">It learns</a>
        </nav>
        <div className="flex items-center gap-2">
          <a href="/login" className="hidden rounded-lg px-3 py-2 text-[14px] font-medium text-white/80 hover:text-white sm:block">
            Log in
          </a>
          <a href="/demo" className="rounded-lg bg-white/10 px-4 py-2 text-[14px] font-semibold text-white ring-1 ring-white/20 backdrop-blur hover:bg-white/15">
            Live demo
          </a>
        </div>
      </div>
    </header>
  );
}

/** The hero's phone: a real-looking Hinglish order, as the customer sees it on WhatsApp. */
function HeroPhone() {
  const bubble = "max-w-[82%] rounded-2xl px-3 py-2 text-[12.5px] leading-snug shadow-sm";
  return (
    <div className="relative mx-auto w-[300px] animate-float">
      <div className="rounded-[38px] bg-[#0f0b2b] p-2.5 shadow-[0_40px_80px_-20px_rgba(0,0,0,0.6)] ring-1 ring-white/10">
        <div className="overflow-hidden rounded-[30px] bg-[#efeae2]">
          <div className="flex items-center gap-2 bg-[#008069] px-4 py-3 text-white">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-white/20">
              <Store size={15} />
            </div>
            <div className="leading-tight">
              <div className="text-[13px] font-semibold">Meera's Bakes</div>
              <div className="text-[10.5px] text-white/80">online · replies instantly</div>
            </div>
          </div>
          <div className="space-y-2 px-3 py-3">
            <div className="flex justify-end">
              <div className={cx(bubble, "rounded-br-md bg-[#d9fdd3] text-[#111]")}>1 kg chocolate truffle chahiye kal shaam, Kothrud 🎂</div>
            </div>
            <div className="flex justify-start">
              <div className={cx(bubble, "rounded-bl-md bg-white text-[#111]")}>
                Ji zaroor! <b>Chocolate Truffle 1 kg</b> — ₹1,050 + ₹50 delivery. Kal 4–8 pm, Kothrud. Confirm karoon?
                <div className="mt-1.5 grid grid-cols-2 gap-1 border-t border-black/5 pt-1.5 text-center text-[12px] font-semibold text-[#027eb5]">
                  <span>Haan, confirm</span>
                  <span>Change</span>
                </div>
              </div>
            </div>
            <div className="flex justify-end">
              <div className={cx(bubble, "rounded-br-md bg-[#d9fdd3] text-[#111]")}>Haan, confirm. UPI se pay karunga</div>
            </div>
            <div className="flex justify-start">
              <div className={cx(bubble, "rounded-bl-md bg-white text-[#111]")}>
                <div className="text-[11px] text-[#667]">Order MB-1042 · ₹1,100</div>
                <div className="mt-1 flex items-center justify-center gap-1.5 rounded-lg bg-[#008069] py-1.5 text-[12px] font-semibold text-white">
                  <CreditCard size={13} /> Pay now
                </div>
                <div className="mt-1 text-right text-[10px] text-[#889]">
                  12:41 <CheckCheck size={11} className="inline text-[#34b7f1]" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      {/* floating proof cards */}
      <div className="absolute -left-[196px] bottom-8 hidden w-52 rounded-2xl bg-white/95 p-3 shadow-pop backdrop-blur xl:block">
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-signal-l text-signal">
            <PhoneCall size={15} />
          </span>
          <div className="leading-tight">
            <div className="text-[12.5px] font-semibold text-ink">Call answered</div>
            <div className="text-[11px] text-stone">in Hindi · photo matched to a cake</div>
          </div>
        </div>
      </div>
      <div className="absolute -right-[184px] top-20 hidden w-48 rounded-2xl bg-white/95 p-3 shadow-pop backdrop-blur xl:block">
        <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-signal">
          <Sparkles size={12} /> Learned from the owner
        </div>
        <div className="mt-1.5 text-[12.5px] leading-snug text-ink">“Cancel myself when delivery is more than 24 h away.”</div>
        <div className="mt-2 flex gap-1.5">
          <span className="rounded-md bg-verdant-l px-1.5 py-0.5 text-[11px] font-semibold text-[#05603a]">Always</span>
          <span className="rounded-md bg-bone px-1.5 py-0.5 text-[11px] text-dusk">undo anytime</span>
        </div>
      </div>
    </div>
  );
}

function Hero() {
  return (
    <section className="bg-hero relative overflow-hidden pb-24 pt-32 text-white">
      <div className="grid-fade pointer-events-none absolute inset-0" />
      <div className="relative mx-auto grid max-w-6xl items-center gap-14 px-5 lg:grid-cols-[1.1fr_1fr]">
        <div className="animate-rise">
          <span className="inline-flex items-center gap-2 rounded-full bg-white/10 px-3 py-1 text-[12.5px] font-medium text-white/85 ring-1 ring-white/15">
            <span className="h-1.5 w-1.5 rounded-full bg-marigold" /> The AI employee for India's small businesses
          </span>
          <h1 className="mt-6 font-display text-[44px] font-bold leading-[1.05] tracking-tight sm:text-[58px]">
            Your shop's
            <br />
            AI employee.
            <br />
            <span className="text-gradient">Bol do. Baaki hum.</span>
          </h1>
          <p className="mt-6 max-w-xl text-[17px] leading-relaxed text-white/75">
            It answers every customer on WhatsApp, Instagram and phone calls — in Hinglish, Hindi or English — takes orders and payments, follows your
            rules, and when it isn't sure, it asks you and learns.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <PrimaryCta href="/demo">
              Meet a live AI employee <ArrowRight size={18} />
            </PrimaryCta>
            <a
              href="/signup"
              className="inline-flex h-12 items-center gap-2 rounded-xl px-5 text-[15px] font-semibold text-white ring-1 ring-white/25 transition hover:bg-white/10"
            >
              Create yours — free
            </a>
          </div>
          <div className="mt-8 flex flex-wrap gap-x-6 gap-y-2 text-[13px] text-white/60">
            <span className="inline-flex items-center gap-1.5">
              <Check size={14} className="text-marigold" /> Ready in 5 minutes from a menu photo
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Check size={14} className="text-marigold" /> Never invents a price
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Check size={14} className="text-marigold" /> You stay in control
            </span>
          </div>
        </div>
        <HeroPhone />
      </div>
    </section>
  );
}

const CHANNELS: { icon: LucideIcon; label: string }[] = [
  { icon: MessageCircle, label: "WhatsApp" },
  { icon: Camera, label: "Instagram" },
  { icon: MessageSquare, label: "Messenger" },
  { icon: Send, label: "Telegram" },
  { icon: Phone, label: "Phone calls" },
  { icon: CreditCard, label: "Razorpay" },
];

function Channels() {
  return (
    <section className="border-b border-sand bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-x-10 gap-y-4 px-5 py-7 text-[14px] font-medium text-dusk">
        <span className="text-[12px] font-semibold uppercase tracking-wider text-stone">Works where your customers are</span>
        {CHANNELS.map(({ icon: Icon, label }) => (
          <span key={label} className="inline-flex items-center gap-2">
            <Icon size={17} className="text-signal" /> {label}
          </span>
        ))}
      </div>
    </section>
  );
}

function SectionTitle({ kicker, title, children }: { kicker: string; title: ReactNode; children?: ReactNode }) {
  return (
    <div className="mx-auto max-w-2xl text-center">
      <div className="text-[13px] font-semibold uppercase tracking-wider text-signal">{kicker}</div>
      <h2 className="mt-2 font-display text-[34px] font-bold leading-tight tracking-tight text-ink sm:text-[40px]">{title}</h2>
      {children && <p className="mt-4 text-[16px] leading-relaxed text-dusk">{children}</p>}
    </div>
  );
}

function HowItWorks() {
  const steps = [
    {
      icon: FileImage,
      title: "Send your menu",
      text: "A photo of your menu or price list is enough. It reads the items, sizes and prices and builds your catalogue — you approve it in one tap.",
    },
    {
      icon: BookOpenCheck,
      title: "Tell it how you work",
      text: "“Delivery ₹50 within 10 km.” “COD only under ₹1,000.” “Ask me before cancelling.” Plain words — it reads them back so you know it understood.",
    },
    {
      icon: Sparkles,
      title: "It works, and learns",
      text: "It answers, sells and takes payments around the clock. When something is new, it asks you — and remembers your answer for next time.",
    },
  ];
  return (
    <section id="how" className="bg-parchment py-24">
      <div className="mx-auto max-w-6xl px-5">
        <SectionTitle kicker="How it works" title="Hire it in five minutes. No training, no forms.">
          Set up happens in a chat with your AI employee — the same way you'd brief a new person in the shop.
        </SectionTitle>
        <div className="mt-14 grid gap-5 md:grid-cols-3">
          {steps.map((s, i) => (
            <div key={s.title} className="relative rounded-2xl bg-white p-7 shadow-card">
              <div className="flex items-center gap-3">
                <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-brand text-white shadow-glow">
                  <s.icon size={20} />
                </span>
                <span className="font-display text-[13px] font-semibold text-stone">Step {i + 1}</span>
              </div>
              <h3 className="mt-5 font-display text-[20px] font-semibold text-ink">{s.title}</h3>
              <p className="mt-2 text-[14.5px] leading-relaxed text-dusk">{s.text}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

const FEATURES: { icon: LucideIcon; title: string; text: string; tone: string }[] = [
  { icon: MessageCircle, title: "Sells in chat", text: "Shows products, answers questions, takes the order with every detail, sends a Razorpay link and confirms when paid.", tone: "bg-[#e7f8ee] text-[#128c4a]" },
  { icon: Mic, title: "Answers phone calls", text: "Picks up WhatsApp calls with a natural voice in Hindi or English. Send a photo mid-call and it finds the matching cake.", tone: "bg-signal-l text-signal" },
  { icon: Brain, title: "Learns from you", text: "Unsure? It asks you with Yes / No buttons, does exactly what you decide, then offers to remember it as a rule.", tone: "bg-blossom text-marigold-d" },
  { icon: Workflow, title: "Follows your procedures", text: "“Remind customers a day before delivery.” Describe it once in words — it becomes a reliable automation.", tone: "bg-sky-l text-sky" },
  { icon: ShieldCheck, title: "Never makes things up", text: "Every price, policy and promise is checked against your own data before it is sent. No invented discounts. Ever.", tone: "bg-verdant-l text-verdant" },
  { icon: Languages, title: "Speaks like your customers", text: "Hinglish, Hindi and English — it replies in the language and script the customer used, with your shop's tone.", tone: "bg-[#fdeaf3] text-[#c2185b]" },
];

function Features() {
  return (
    <section id="features" className="bg-white py-24">
      <div className="mx-auto max-w-6xl px-5">
        <SectionTitle kicker="Features" title={<>Everything a great shop assistant does. <span className="text-signal">Without a day off.</span></>} />
        <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f) => (
            <div key={f.title} className="group rounded-2xl border border-sand bg-white p-6 transition hover:-translate-y-1 hover:shadow-pop">
              <span className={cx("flex h-11 w-11 items-center justify-center rounded-xl", f.tone)}>
                <f.icon size={20} />
              </span>
              <h3 className="mt-5 font-display text-[18px] font-semibold text-ink">{f.title}</h3>
              <p className="mt-2 text-[14.5px] leading-relaxed text-dusk">{f.text}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Learning() {
  const steps = [
    { who: "Customer", text: "“Mera order cancel kar do, plans change ho gaye.”", tone: "bg-white text-ink" },
    { who: "AI → Owner", text: "Taha wants to cancel MB-1042 (₹1,100, paid, delivery Thursday). I suggest cancelling.", tone: "bg-signal-l text-ink", buttons: ["Yes, cancel", "No"] },
    { who: "Owner taps “Always”", text: "“Cancel myself when delivery is more than 24 h away and it's confirmed.”", tone: "bg-blossom text-ink", buttons: ["Always", "Only for Taha", "Just this once"] },
    { who: "Next customer", text: "Cancelled straight away. The owner gets a short note citing their own rule.", tone: "bg-verdant-l text-ink" },
  ];
  return (
    <section id="learning" className="bg-hero relative overflow-hidden py-24 text-white">
      <div className="grid-fade pointer-events-none absolute inset-0" />
      <div className="relative mx-auto max-w-6xl px-5">
        <div className="mx-auto max-w-2xl text-center">
          <div className="text-[13px] font-semibold uppercase tracking-wider text-marigold">It learns your way</div>
          <h2 className="mt-2 font-display text-[34px] font-bold leading-tight tracking-tight sm:text-[40px]">
            We didn't hard-code your rules. <span className="text-gradient">It learns them from you.</span>
          </h2>
          <p className="mt-4 text-[16px] leading-relaxed text-white/70">
            Every shop works differently. Instead of guessing, it asks — once — and keeps your answer as a rule you can read, undo or change.
          </p>
        </div>
        <div className="mt-14 grid gap-4 lg:grid-cols-4">
          {steps.map((s, i) => (
            <div key={s.who} className="relative rounded-2xl bg-white/[0.06] p-5 ring-1 ring-white/10 backdrop-blur">
              <div className="flex items-center gap-2 text-[12px] font-semibold uppercase tracking-wide text-white/60">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-white/10 text-[11px] text-white">{i + 1}</span>
                {s.who}
              </div>
              <div className={cx("mt-3 rounded-xl px-3.5 py-3 text-[13.5px] leading-snug shadow-sm", s.tone)}>
                {s.text}
                {s.buttons && (
                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    {s.buttons.map((b, j) => (
                      <span key={b} className={cx("rounded-md px-2 py-0.5 text-[12px] font-semibold", j === 0 ? "bg-signal text-white" : "bg-white text-signal ring-1 ring-signal/30")}>
                        {b}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Proof() {
  const stats = [
    { value: "164", label: "test conversations on every build" },
    { value: "100%", label: "of invented prices and discounts blocked" },
    { value: "40/40", label: "right tool for the job — 60% in Hinglish" },
    { value: "< 10 s", label: "typical reply, 95% of the time" },
  ];
  return (
    <section className="bg-white py-20">
      <div className="mx-auto max-w-6xl px-5">
        <div className="grid items-center gap-10 lg:grid-cols-[1fr_1.3fr]">
          <div>
            <div className="text-[13px] font-semibold uppercase tracking-wider text-signal">Measured, not claimed</div>
            <h2 className="mt-2 font-display text-[32px] font-bold leading-tight tracking-tight text-ink">Trust you can check.</h2>
            <p className="mt-4 text-[15.5px] leading-relaxed text-dusk">
              Every build is tested with real customer conversations in Hinglish, Hindi and English, and every reply passes a guard that checks
              prices, links and promises against your data before it is sent.
            </p>
            <div className="mt-6 flex items-center gap-2 text-[14px] font-medium text-verdant">
              <BadgeCheck size={18} /> Latest build: all 8 quality suites passing
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            {stats.map((s) => (
              <div key={s.label} className="rounded-2xl bg-parchment p-6">
                <div className="font-display text-[34px] font-bold tracking-tight text-ink">{s.value}</div>
                <div className="mt-1 text-[13.5px] text-dusk">{s.label}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function FinalCta() {
  return (
    <section className="bg-parchment py-20">
      <div className="mx-auto max-w-5xl px-5">
        <div className="bg-brand relative overflow-hidden rounded-3xl px-8 py-14 text-center text-white shadow-glow sm:px-14">
          <h2 className="font-display text-[32px] font-bold leading-tight tracking-tight sm:text-[40px]">Talk to a live AI employee — right now.</h2>
          <p className="mx-auto mt-4 max-w-xl text-[16px] text-white/85">
            Chat with it or call it from your browser as a customer, and watch the owner's side as it asks, decides and learns.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <button onClick={go("/demo")} className="inline-flex h-12 items-center gap-2 rounded-xl bg-white px-6 text-[15px] font-semibold text-signal-d shadow-lg transition hover:-translate-y-0.5">
              Open the live demo <ArrowRight size={18} />
            </button>
            <button onClick={go("/signup")} className="inline-flex h-12 items-center gap-2 rounded-xl px-6 text-[15px] font-semibold text-white ring-1 ring-white/40 transition hover:bg-white/10">
              Create your own
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-sand bg-white">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-5 py-8 text-[13px] text-stone">
        <Logo />
        <span>Built for the shops that keep India running · © 2026 Reflexo</span>
        <a href="/admin" className="hover:text-dusk">Operations console</a>
      </div>
    </footer>
  );
}

export function Landing() {
  return (
    <div className="min-h-full bg-white">
      <Nav />
      <Hero />
      <Channels />
      <HowItWorks />
      <Features />
      <Learning />
      <Proof />
      <FinalCta />
      <Footer />
    </div>
  );
}
