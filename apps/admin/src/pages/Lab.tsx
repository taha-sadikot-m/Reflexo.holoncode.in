import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, CircleCheck, CircleX, CornerDownLeft, CreditCard, Loader2, Play, Send, SkipForward, Square, Store } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { Badge, Button, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type ScenarioRun, type ScenarioSummary, type SimThread, type SimThreadItem } from "../lib/api";
import { useSession } from "../lib/session";
import { fmtTime } from "../lib/stages";

// ---------------------------------------------------------------- rendering a provider payload as the phone shows it

type Option = { id: string; title: string };

function payloadText(p: Record<string, any>): string {
  if (p.type === "text") return p.text?.body ?? "";
  if (p.type === "interactive") return p.interactive?.body?.text ?? "";
  if (p.type === "image") return p.image?.caption ? `[Photo] ${p.image.caption}` : "[Photo]";
  if (p.type === "document") return `[File] ${p.document?.filename ?? "file"}`;
  if (p.type === "template") return `[Template] ${p.template?.name}`;
  if (typeof p.text === "string") return p.text; // telegram-shaped
  return JSON.stringify(p).slice(0, 200);
}

function payloadOptions(p: Record<string, any>): Option[] {
  const action = p.interactive?.action;
  if (!action) return [];
  if (action.buttons) return action.buttons.map((b: any) => ({ id: b.reply.id, title: b.reply.title }));
  return (action.sections ?? []).flatMap((s: any) => (s.rows ?? []).map((r: any) => ({ id: r.id, title: r.title })));
}

function Bubble({
  item,
  onTap,
  disabled,
}: {
  item: SimThreadItem;
  onTap: (o: Option) => void;
  disabled: boolean;
}) {
  const mine = item.from !== "business";
  if (mine) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-[#d9fdd3] px-3 py-1.5 text-[13px] text-ink shadow-sm">
          {item.option ? (
            <span className="inline-flex items-center gap-1 italic">
              <CornerDownLeft size={12} /> {item.option}
            </span>
          ) : (
            <span className="whitespace-pre-wrap break-words">{item.text}</span>
          )}
          <div className="mt-0.5 text-right text-[10px] text-stone">{fmtTime(item.at)}</div>
        </div>
      </div>
    );
  }
  const p = item.payload ?? {};
  const options = payloadOptions(p);
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-sm bg-white px-3 py-1.5 text-[13px] text-ink shadow-sm">
        <div className="whitespace-pre-wrap break-words">{payloadText(p)}</div>
        {options.length > 0 && (
          <div className="mt-1.5 flex flex-col gap-1 border-t border-sand pt-1.5">
            {options.map((o) => (
              <button
                key={o.id}
                disabled={disabled}
                onClick={() => onTap(o)}
                className="rounded-lg py-1 text-center text-[13px] font-semibold text-[#027eb5] hover:bg-bone disabled:opacity-50"
              >
                {o.title}
              </button>
            ))}
          </div>
        )}
        <div className="mt-0.5 flex items-center justify-end gap-1 text-[10px] text-stone">
          {fmtTime(item.at)}
          {item.status === "failed" ? <AlertTriangle size={11} className="text-ember" /> : item.status === "sent" ? <Check size={11} /> : null}
        </div>
      </div>
    </div>
  );
}

function Device({
  title,
  subtitle,
  thread,
  error,
  onSend,
  onTap,
  onPay,
  busy,
}: {
  title: string;
  subtitle?: React.ReactNode;
  thread: SimThread | undefined;
  error: unknown;
  onSend: (text: string) => void;
  onTap: (o: Option) => void;
  onPay?: (id: string) => void;
  busy: boolean;
}) {
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const count = thread?.items.length ?? 0;
  useEffect(() => {
    // scroll the chat only (scrollIntoView also scrolled the whole page), and return nothing (see Conversations)
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [count]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!draft.trim()) return;
    onSend(draft.trim());
    setDraft("");
  };
  const unpaid = (thread?.payment_links ?? []).filter((l) => l.status === "created");
  return (
    <div className="min-w-0 flex-1 rounded-[34px] bg-white p-2 shadow-pop ring-1 ring-sand">
    <div className="flex h-[640px] flex-col overflow-hidden rounded-[26px] bg-[#efeae2]">
      <div className="flex items-center gap-2.5 bg-[#008069] px-4 py-2.5 text-white">
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-white/20">
          <Store size={16} />
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{title}</div>
          <div className="truncate text-[11px] opacity-80">{subtitle}</div>
        </div>
      </div>
      <div ref={scroller} className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 py-3">
        <ErrorNote error={error} />
        {thread?.items.length ? (
          thread.items.map((it, i) => <Bubble key={i} item={it} onTap={onTap} disabled={busy} />)
        ) : (
          <div className="py-16 text-center text-xs text-stone">No messages yet — say hi</div>
        )}
      </div>
      {onPay && unpaid.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-sand bg-white/70 px-3 py-1.5">
          {unpaid.map((l) => (
            <Button key={l.id} variant="ghost" className="text-xs" onClick={() => onPay(l.id)} disabled={busy}>
              <CreditCard size={13} /> Pay {l.order_number} · ₹{(l.amount_paise / 100).toLocaleString("en-IN")}
            </Button>
          ))}
        </div>
      )}
      <form onSubmit={submit} className="flex items-center gap-2 bg-[#f0f2f5] px-2 py-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Message"
          className="min-w-0 flex-1 rounded-full bg-white px-4 py-2 text-sm outline-none"
        />
        <button type="submit" disabled={busy || !draft.trim()} className="rounded-full bg-[#008069] p-2 text-white disabled:opacity-50">
          <Send size={16} />
        </button>
      </form>
    </div>
    </div>
  );
}

// ---------------------------------------------------------------- scenarios

const STEP_ICON: Record<string, React.ReactNode> = {
  ok: <CircleCheck size={14} className="text-verdant" />,
  failed: <CircleX size={14} className="text-ember" />,
  running: <Loader2 size={14} className="animate-spin text-signal" />,
  pending: <span className="inline-block h-3.5 w-3.5 rounded-full border border-sand" />,
  skipped: <SkipForward size={14} className="text-stone" />,
};

function RunView({ run, onStop }: { run: ScenarioRun; onStop: () => void }) {
  const tone = run.status === "passed" ? "good" : run.status === "failed" ? "bad" : run.status === "running" ? "info" : "neutral";
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{run.title}</div>
          <Mono>actors: {Object.entries(run.actors).map(([n, a]) => `${n} #${a}`).join(" · ")}</Mono>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={tone}>{run.status}</Badge>
          {run.status === "running" && (
            <Button variant="ghost" className="text-xs" onClick={onStop}>
              <Square size={12} /> Stop
            </Button>
          )}
        </div>
      </div>
      <ol className="space-y-1">
        {run.steps.map((s) => (
          <li key={s.index} className={cx("rounded-lg px-2 py-1 text-xs", s.status === "failed" && "bg-ember-l", s.status === "running" && "bg-signal-l")}>
            <div className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0">{STEP_ICON[s.status]}</span>
              <span className="min-w-0 flex-1 break-words">{s.label}</span>
              {s.seconds != null && <Mono className="shrink-0">{s.seconds}s</Mono>}
            </div>
            {s.detail && s.status !== "pending" && <div className="ml-6 mt-0.5 line-clamp-2 break-words text-[11px] text-dusk">{s.detail}</div>}
          </li>
        ))}
      </ol>
    </div>
  );
}

export function Lab() {
  const { business } = useSession();
  const qc = useQueryClient();
  const bid = business?.id ?? "";
  const [runId, setRunId] = useState<string | null>(null);
  const [actor, setActor] = useState<number>(1);
  const [actorName, setActorName] = useState<string>("Customer 1");

  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: () => api.get<{ scenarios: ScenarioSummary[]; runs: ScenarioRun[] }>("/scenarios"),
    refetchInterval: 10000,
  });
  const run = useQuery({
    queryKey: ["scenario-run", runId],
    queryFn: () => api.get<ScenarioRun>(`/scenarios/runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  const customer = useQuery({
    queryKey: ["sim-thread", bid, actor],
    queryFn: () => api.get<SimThread>("/sim/thread", { business_id: bid, actor: String(actor) }),
    enabled: !!bid,
    refetchInterval: 1500,
  });
  const owner = useQuery({
    queryKey: ["sim-owner", bid],
    queryFn: () => api.get<SimThread>("/sim/owner/thread", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 1500,
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["sim-thread", bid] });
    void qc.invalidateQueries({ queryKey: ["sim-owner", bid] });
  };
  const sendCustomer = useMutation({
    mutationFn: (body: { text?: string; option_id?: string; option_title?: string }) =>
      api.post("/sim/send", { actor, ...body }, { business_id: bid }),
    onSettled: refresh,
  });
  const sendOwner = useMutation({
    mutationFn: (body: { text?: string; option_id?: string; option_title?: string }) =>
      api.post("/sim/owner/send", body, { business_id: bid }),
    onSettled: refresh,
  });
  const pay = useMutation({
    mutationFn: (id: string) => api.post(`/sim/razorpay/pay/${id}`, {}, { business_id: bid }),
    onSettled: refresh,
  });
  const start = useMutation({
    mutationFn: (id: string) => api.post<{ id: string }>(`/scenarios/${id}/run`, {}, { business_id: bid }),
    onSuccess: (r) => setRunId(r.id),
    onSettled: () => qc.invalidateQueries({ queryKey: ["scenarios"] }),
  });
  const stop = useMutation({ mutationFn: (id: string) => api.post(`/scenarios/runs/${id}/stop`) });

  // follow the running scenario's first customer on the customer device
  const live = run.data;
  useEffect(() => {
    if (live && live.status === "running") {
      const [name, num] = Object.entries(live.actors)[0] ?? [];
      if (name && num && num !== actor) {
        setActor(num);
        setActorName(name);
      }
    }
  }, [live?.id, live?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const actorChoices: [string, number][] = [
    ["Customer 1", 1],
    ["Customer 2", 2],
    ["Customer 3", 3],
    ...(live ? Object.entries(live.actors) : []),
  ];
  const running = live?.status === "running";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-ink">Simulation Lab</h1>
          <p className="max-w-3xl text-sm text-dusk">
            Simulated customers and a simulated owner talk to the real pipeline through signed provider payloads — only the last hop to WhatsApp is
            swapped. Nothing here reaches a real phone.
          </p>
        </div>
        <div className="flex gap-3 text-xs font-semibold">
          <Link to="/xray" className="text-signal hover:underline">Open X-ray →</Link>
          <Link to="/chaos" className="text-signal hover:underline">Chaos →</Link>
        </div>
      </div>
      <ErrorNote error={start.error ?? sendCustomer.error ?? sendOwner.error ?? pay.error} />

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[340px_1fr]">
        <div className="space-y-5">
          <Card title="Scenarios">
            {scenarios.data?.scenarios.length ? (
              <ul className="space-y-2">
                {scenarios.data.scenarios.map((s) => (
                  <li key={s.id} className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-sm font-semibold">{s.title}</div>
                      <div className="text-xs text-dusk">{s.covers}</div>
                    </div>
                    <Button className="shrink-0 text-xs" disabled={running || start.isPending} onClick={() => start.mutate(s.id)}>
                      <Play size={12} /> Run
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>No scenarios in config/scenarios.</Empty>
            )}
          </Card>
          {live && (
            <Card title="Run">
              <RunView run={live} onStop={() => stop.mutate(live.id)} />
            </Card>
          )}
          {!live && !!scenarios.data?.runs.length && (
            <Card title="Recent runs">
              <ul className="space-y-1 text-xs">
                {scenarios.data.runs.map((r) => (
                  <li key={r.id} className="flex items-center justify-between">
                    <button className="truncate text-left hover:underline" onClick={() => setRunId(r.id)}>
                      {r.title}
                    </button>
                    <Badge tone={r.status === "passed" ? "good" : r.status === "failed" ? "bad" : "neutral"}>{r.status}</Badge>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>

        <div className="flex flex-col gap-5 lg:flex-row">
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <select
              aria-label="Customer device"
              value={actor}
              onChange={(e) => {
                const n = Number(e.target.value);
                setActor(n);
                setActorName(actorChoices.find(([, a]) => a === n)?.[0] ?? `Customer ${n}`);
              }}
              className="h-9 rounded-md bg-white px-3 text-[13px] font-medium text-ink shadow-sm ring-1 ring-inset ring-sand focus:outline-none focus:ring-2 focus:ring-signal/50"
            >
              {actorChoices.map(([name, n]) => (
                <option key={`${name}-${n}`} value={n}>
                  {name} (#{n})
                </option>
              ))}
            </select>
            <Device
              title={actorName}
              subtitle="WhatsApp · simulated customer"
              thread={customer.data}
              error={customer.error}
              busy={sendCustomer.isPending || pay.isPending}
              onSend={(text) => sendCustomer.mutate({ text })}
              onTap={(o) => sendCustomer.mutate({ option_id: o.id, option_title: o.title })}
              onPay={(id) => pay.mutate(id)}
            />
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <div className="py-1.5 text-sm font-semibold text-dusk">Owner device (simulated)</div>
            <Device
              title="Meera (owner)"
              subtitle="WhatsApp · simulated owner binding"
              thread={owner.data}
              error={owner.error}
              busy={sendOwner.isPending}
              onSend={(text) => sendOwner.mutate({ text })}
              onTap={(o) => sendOwner.mutate({ option_id: o.id, option_title: o.title })}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
