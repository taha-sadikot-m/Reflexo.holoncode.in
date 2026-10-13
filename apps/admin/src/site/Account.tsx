// Sign up (a new shop + its owner's login, then set-up by chat) and log in. Both land in the owner's app (/app).
import { ArrowRight, Check, Loader2, Sparkles } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";

import { Logo } from "../components/brand";
import { cx } from "../components/ui";

async function post(path: string, body: unknown): Promise<void> {
  const resp = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const problem = await resp.json().catch(() => ({}));
    const detail = typeof problem.detail === "string" ? problem.detail : Array.isArray(problem.detail) ? "Please check the form." : null;
    throw new Error(detail ?? "Something went wrong — please try again.");
  }
}

function Shell({ title, sub, children, aside }: { title: string; sub: ReactNode; children: ReactNode; aside: ReactNode }) {
  return (
    <div className="grid min-h-full lg:grid-cols-[1fr_1.05fr]">
      <div className="bg-hero relative hidden overflow-hidden text-white lg:block">
        <div className="grid-fade pointer-events-none absolute inset-0" />
        <div className="relative flex h-full flex-col px-12 py-10">
          <a href="/">
            <Logo dark />
          </a>
          <div className="my-auto max-w-md">{aside}</div>
          <div className="text-[12.5px] text-white/50">© 2026 Reflexo · Built for the shops that keep India running</div>
        </div>
      </div>
      <div className="flex flex-col bg-white">
        <div className="flex h-16 items-center justify-between px-6 lg:hidden">
          <a href="/">
            <Logo />
          </a>
        </div>
        <div className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-6 py-12">
          <h1 className="font-display text-[30px] font-bold leading-tight tracking-tight text-ink">{title}</h1>
          <div className="mt-2 text-[14.5px] text-dusk">{sub}</div>
          <div className="mt-8">{children}</div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, hint, ...input }: { label: string; hint?: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="block">
      <span className="text-[13px] font-medium text-soot">{label}</span>
      <input
        {...input}
        className="mt-1.5 h-11 w-full rounded-xl border border-sand bg-white px-3.5 text-[14.5px] text-ink outline-none transition placeholder:text-stone focus:border-signal focus:ring-4 focus:ring-signal/15"
      />
      {hint && <span className="mt-1 block text-[12px] text-stone">{hint}</span>}
    </label>
  );
}

function Submit({ busy, children }: { busy: boolean; children: ReactNode }) {
  return (
    <button
      type="submit"
      disabled={busy}
      className="inline-flex h-12 w-full items-center justify-center gap-2 rounded-xl bg-signal text-[15px] font-semibold text-white shadow-glow transition hover:bg-signal-d disabled:opacity-70"
    >
      {busy ? <Loader2 size={18} className="animate-spin" /> : null}
      {children}
    </button>
  );
}

export function Signup() {
  const [form, setForm] = useState({ business_name: "", category: "", area: "", owner_name: "", email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/accounts/signup", form);
      window.location.href = "/app/setup";
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };
  return (
    <Shell
      title="Hire your AI employee"
      sub={
        <>
          Free while in preview. Already have an account?{" "}
          <a href="/login" className="font-medium text-signal hover:underline">
            Log in
          </a>
        </>
      }
      aside={
        <>
          <div className="inline-flex items-center gap-2 rounded-full bg-white/10 px-3 py-1 text-[12.5px] text-white/85 ring-1 ring-white/15">
            <Sparkles size={13} className="text-marigold" /> Set up takes about five minutes
          </div>
          <h2 className="mt-5 font-display text-[34px] font-bold leading-tight tracking-tight">
            Brief it like a new person in the shop. <span className="text-gradient">It does the rest.</span>
          </h2>
          <ul className="mt-8 space-y-4 text-[15px] text-white/80">
            {[
              "Send a photo of your menu or price list — it builds your catalogue.",
              "Answer a few questions in plain words — delivery, payments, timings.",
              "Test it as a customer, by chat and by voice — then go live.",
            ].map((t) => (
              <li key={t} className="flex gap-3">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-marigold/20 text-marigold">
                  <Check size={13} />
                </span>
                {t}
              </li>
            ))}
          </ul>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Shop name" required maxLength={80} value={form.business_name} onChange={set("business_name")} placeholder="Sharma Sweets" />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="What you sell" maxLength={80} value={form.category} onChange={set("category")} placeholder="Sweets & namkeen" />
          <Field label="City / area" maxLength={120} value={form.area} onChange={set("area")} placeholder="Indore" />
        </div>
        <Field label="Your name" required maxLength={60} value={form.owner_name} onChange={set("owner_name")} placeholder="Ravi" />
        <Field label="Email" type="email" required autoComplete="email" maxLength={200} value={form.email} onChange={set("email")} placeholder="you@shop.in" />
        <Field
          label="Password"
          type="password"
          required
          minLength={8}
          autoComplete="new-password"
          maxLength={200}
          value={form.password}
          onChange={set("password")}
          hint="At least 8 characters."
        />
        {error && <div className="rounded-xl bg-ember-l px-3.5 py-2.5 text-[13.5px] text-ember">{error}</div>}
        <Submit busy={busy}>
          Create my AI employee <ArrowRight size={17} />
        </Submit>
        <p className="text-center text-[12px] text-stone">Your shop starts on test phones in your dashboard — nothing reaches real customers until you connect WhatsApp.</p>
      </form>
    </Shell>
  );
}

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/accounts/login", { email, password });
      window.location.href = "/app";
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };
  return (
    <Shell
      title="Welcome back"
      sub={
        <>
          New here?{" "}
          <a href="/signup" className="font-medium text-signal hover:underline">
            Create your AI employee
          </a>{" "}
          or{" "}
          <a href="/demo" className="font-medium text-signal hover:underline">
            try the live demo
          </a>
          .
        </>
      }
      aside={
        <h2 className="font-display text-[34px] font-bold leading-tight tracking-tight">
          Your shop kept running while you were away. <span className="text-gradient">See what it handled.</span>
        </h2>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Email" type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <Field label="Password" type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <div className={cx("rounded-xl bg-ember-l px-3.5 py-2.5 text-[13.5px] text-ember")}>{error}</div>}
        <Submit busy={busy}>Log in</Submit>
      </form>
    </Shell>
  );
}
