import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FastForward, Pause, Play } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Json, PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type AutomationDetail, type AutomationRow, type AutomationRun } from "../lib/api";
import { useSession } from "../lib/session";

type Step = { id: string; type?: string; [k: string]: unknown };

const STEP_ICON: Record<string, string> = {
  send: "💬",
  wait: "⏱",
  wait_for_event: "👂",
  branch: "🔀",
  counter_limit: "🔢",
  claim_once: "☝️",
  deny: "⛔",
  llm_classify: "🏷",
  llm_generate: "✍️",
  notify_owner: "🔔",
  ask_owner: "🙋",
  update_order: "🧾",
  set_tag: "🏷",
  end: "🏁",
};

// outgoing edges of a step: the keys of the definition language that name another step
const EDGE_KEYS: [string, string][] = [
  ["next", "next"],
  ["then", "then"],
  ["else", "else"],
  ["else_", "else"],
  ["on_timeout", "on timeout"],
  ["goto", "goto"],
];

function stepFacts(s: Step): string[] {
  const skip = new Set(["id", "type", ...EDGE_KEYS.map(([k]) => k)]);
  return Object.entries(s)
    .filter(([k, v]) => !skip.has(k) && v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
}

function Diagram({ def, active }: { def: AutomationDetail["definition"]; active: Set<string> }) {
  const steps = (def.steps ?? []) as Step[];
  return (
    <div className="space-y-1.5">
      <div className="rounded-lg border border-dashed border-signal/50 bg-signal-l px-3 py-2 text-xs dark:bg-[#26214d]">
        <span className="font-semibold">⚡ Trigger</span> <Mono>{JSON.stringify(def.trigger)}</Mono>
      </div>
      {steps.map((s, i) => {
        const edges = EDGE_KEYS.filter(([k]) => typeof s[k] === "string").map(([k, label]) => [label, String(s[k])]);
        const falls = !edges.length && s.type !== "deny" && s.type !== "end" && i < steps.length - 1;
        return (
          <div key={s.id} id={`step-${s.id}`}>
            <div className="ml-4 h-2 border-l border-sand dark:border-twilight" />
            <div className={cx("rounded-lg border px-3 py-2", active.has(s.id) ? "border-sun bg-sun-l dark:bg-[#3d3210]" : "border-sand dark:border-twilight")}>
              <div className="flex items-center gap-2 text-sm">
                <span>{STEP_ICON[s.type ?? ""] ?? "▫️"}</span>
                <span className="font-semibold">{s.id}</span>
                <Mono>{s.type}</Mono>
                {active.has(s.id) && <Badge tone="warn">a run is here</Badge>}
              </div>
              {stepFacts(s).map((f) => (
                <div key={f} className="ml-6 break-words text-xs text-dusk dark:text-stone">
                  {f}
                </div>
              ))}
              {(edges.length > 0 || falls) && (
                <div className="ml-6 mt-1 flex flex-wrap gap-2 text-[11px]">
                  {edges.map(([label, to]) => (
                    <a key={label} href={`#step-${to}`} className="rounded bg-bone px-1.5 py-0.5 hover:bg-sand dark:bg-twilight">
                      {label} → {to}
                    </a>
                  ))}
                  {falls && <span className="text-stone">↓ {steps[i + 1]?.id}</span>}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RunRow({ r, onSkip, busy }: { r: AutomationRun; onSkip: () => void; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const live = r.status === "running" || r.status === "waiting";
  const waiting = r.waits.filter((w) => w.status === "waiting" || w.status === "pending");
  return (
    <li className="border-t border-sand/60 py-2 dark:border-twilight">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <button onClick={() => setOpen(!open)} className="font-mono text-xs hover:underline">
          {open ? "▾" : "▸"} {r.instance_key}
        </button>
        <StatusBadge status={r.status} />
        <span className="text-xs text-stone">v{r.version} · {fmtWhen(r.started_at)}</span>
        {waiting.map((w, i) => (
          <Badge key={i} tone="warn">
            waits for {w.event_type} {w.expires_at && `· until ${fmtWhen(w.expires_at)}`}
          </Badge>
        ))}
        {live && r.workflow_id && (
          <Button variant="ghost" className="ml-auto text-xs" onClick={onSkip} disabled={busy} title="Fast-forward the current wait (Temporal signal skip_wait)">
            <FastForward size={12} /> Skip wait
          </Button>
        )}
      </div>
      {open && (
        <div className="mt-2 space-y-1 pl-4">
          {r.workflow_id && <div className="text-[11px] text-stone">workflow <Mono>{r.workflow_id}</Mono></div>}
          {r.steps.map((s, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2 text-xs">
              <StatusBadge status={s.status} />
              <Mono>{s.step_id}</Mono>
              <span className="text-stone">{fmtWhen(s.finished_at ?? s.started_at)}</span>
              {s.output != null && <span className="max-w-full truncate text-dusk dark:text-stone">{JSON.stringify(s.output)}</span>}
              {s.error != null && <span className="text-ember">{JSON.stringify(s.error)}</span>}
            </div>
          ))}
          {!r.steps.length && <div className="text-xs text-stone">No steps recorded (instant rule or not started).</div>}
          {r.error && <Json value={r.error} />}
        </div>
      )}
    </li>
  );
}

function Detail({ id, bid }: { id: string; bid: string }) {
  const qc = useQueryClient();
  const [showJson, setShowJson] = useState(false);
  const q = useQuery({
    queryKey: ["automation", bid, id],
    queryFn: () => api.get<AutomationDetail>(`/automations/${id}`, { business_id: bid }),
    refetchInterval: 3000,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["automation", bid, id] });
    void qc.invalidateQueries({ queryKey: ["automations", bid] });
  };
  const toggle = useMutation({
    mutationFn: (to: "pause" | "resume") => api.post(`/automations/${id}/${to}`, {}, { business_id: bid }),
    onSettled: refresh,
  });
  const skip = useMutation({
    mutationFn: (runId: string) => api.post(`/automation-runs/${runId}/skip-wait`, {}, { business_id: bid }),
    onSettled: refresh,
  });
  const a = q.data;
  if (q.error) return <ErrorNote error={q.error} />;
  if (!a) return <Empty>Loading…</Empty>;
  const active = new Set(
    a.runs.filter((r) => r.status === "running" || r.status === "waiting").map((r) => r.steps.at(-1)?.step_id ?? "").filter(Boolean),
  );
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-[17px] font-semibold tracking-tight text-ink">{a.name}</h2>
        <StatusBadge status={a.status} />
        <Badge>v{a.version}</Badge>
        <Badge tone="info">{a.executor}</Badge>
        <div className="ml-auto">
          {a.status === "active" ? (
            <Button variant="ghost" className="text-xs" onClick={() => toggle.mutate("pause")} disabled={toggle.isPending}>
              <Pause size={12} /> Pause
            </Button>
          ) : (
            <Button className="text-xs" onClick={() => toggle.mutate("resume")} disabled={toggle.isPending}>
              <Play size={12} /> Resume
            </Button>
          )}
        </div>
      </div>
      <ErrorNote error={toggle.error ?? skip.error} />
      {a.description && <div className="text-sm italic text-dusk dark:text-stone">Owner said: “{a.description}”</div>}
      {a.readback && <div className="whitespace-pre-wrap rounded-lg bg-blossom px-3 py-2 text-sm dark:bg-[#3d2a10]">{a.readback}</div>}
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-stone">Flow</span>
          <button className="text-xs text-signal hover:underline" onClick={() => setShowJson(!showJson)}>
            {showJson ? "Diagram" : "Definition JSON"}
          </button>
        </div>
        {showJson ? <Json value={a.definition} /> : <Diagram def={a.definition} active={active} />}
      </div>
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wide text-stone">Runs</div>
        {a.runs.length ? (
          <ul>
            {a.runs.map((r) => (
              <RunRow key={r.id} r={r} onSkip={() => skip.mutate(r.id)} busy={skip.isPending} />
            ))}
          </ul>
        ) : (
          <div className="py-2 text-xs text-stone">No runs yet.</div>
        )}
      </div>
    </div>
  );
}

export function Automations() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const [params, setParams] = useSearchParams();
  const selected = params.get("a");
  const list = useQuery({
    queryKey: ["automations", bid],
    queryFn: () => api.get<AutomationRow[]>("/automations", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  return (
    <div className="space-y-5">
      <PageHeader title="Automations">Procedures the owner described in their own words — each compiled to a checked definition, read back, and run durably.</PageHeader>
      <ErrorNote error={list.error} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[400px_1fr]">
        <Card>
          {list.data?.length ? (
            <ul className="space-y-1">
              {list.data.map((a) => (
                <li key={a.id}>
                  <button
                    onClick={() => setParams({ a: a.id }, { replace: true })}
                    className={cx(
                      "w-full rounded-lg px-3 py-2 text-left hover:bg-bone dark:hover:bg-twilight",
                      selected === a.id && "bg-signal-l dark:bg-[#26214d]",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate text-sm font-semibold">{a.name}</span>
                      <StatusBadge status={a.status} />
                    </div>
                    <div className="mt-0.5 line-clamp-2 text-xs text-dusk dark:text-stone">{a.readback ?? a.description}</div>
                    <div className="mt-1 flex gap-3 text-[11px] text-stone">
                      <span>v{a.version}</span>
                      <span>{a.executor}</span>
                      <span>{a.runs_today} runs today</span>
                      {a.live_runs > 0 && <span className="text-sun">{a.live_runs} live</span>}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No automations yet — the owner creates them by describing a procedure on WhatsApp.</Empty>
          )}
        </Card>
        <Card>{selected ? <Detail id={selected} bid={bid} /> : <Empty>Pick an automation.</Empty>}</Card>
      </div>
    </div>
  );
}
