import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleX, Loader2, Play, SkipForward, Square } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Device } from "../components/phone";
import { Badge, Button, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type ScenarioRun, type ScenarioSummary, type SimThread, type SimMediaRef, uploadSimMedia } from "../lib/api";
import { useSession } from "../lib/session";

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
    mutationFn: (body: { text?: string; option_id?: string; option_title?: string; media?: SimMediaRef }) =>
      api.post("/sim/send", { actor, ...body }, { business_id: bid }),
    onSettled: refresh,
  });
  const sendOwner = useMutation({
    mutationFn: (body: { text?: string; option_id?: string; option_title?: string; media?: SimMediaRef }) =>
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
          <Link to="/quality/xray" className="text-signal hover:underline">Open X-ray →</Link>
          <Link to="/quality/chaos" className="text-signal hover:underline">Chaos →</Link>
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
              className="flex-1"
              title={actorName}
              subtitle="WhatsApp · simulated customer"
              thread={customer.data}
              error={customer.error}
              busy={sendCustomer.isPending || pay.isPending}
              onSend={(text) => sendCustomer.mutate({ text })}
              onTap={(o) => sendCustomer.mutate({ option_id: o.id, option_title: o.title })}
              onPay={(id) => pay.mutate(id)}
              onAttach={async (file, caption) => {
                await sendCustomer.mutateAsync({ media: await uploadSimMedia(bid, file), text: caption || undefined });
              }}
            />
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <div className="py-1.5 text-sm font-semibold text-dusk">Owner device (simulated)</div>
            <Device
              className="flex-1"
              title="Meera (owner)"
              subtitle="WhatsApp · simulated owner binding"
              thread={owner.data}
              error={owner.error}
              busy={sendOwner.isPending}
              onSend={(text) => sendOwner.mutate({ text })}
              onTap={(o) => sendOwner.mutate({ option_id: o.id, option_title: o.title })}
              onAttach={async (file, caption) => {
                await sendOwner.mutateAsync({ media: await uploadSimMedia(bid, file), text: caption || undefined });
              }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
