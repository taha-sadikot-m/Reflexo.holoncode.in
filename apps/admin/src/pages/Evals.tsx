import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useState } from "react";

import { PageHeader, fmtWhen } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api } from "../lib/api";

interface EvalRun {
  id: string;
  build_ref: string;
  suite: string;
  cases: number;
  passed: number;
  metrics: Record<string, number | null> & { threshold: number; score: number };
  failed: { id: string; detail: string }[];
  created_at: string;
}

interface Overview {
  runs: EvalRun[];
  running: boolean;
  log: string[];
  suites: string[];
}

const ABOUT: Record<string, string> = {
  tool_selection: "Customer desk calls the right tools (and not the wrong ones) — 60 % Hinglish",
  gate_adversarial: "Forced wrong prices, invented discounts, foreign links/numbers are all blocked",
  grounding: "Business facts are cited and correct",
  language: "Replies in the customer's language and script",
  ask_owner_scope: "Owner answers saved for everyone / this customer / today",
  owner_commands: "Owner commands reach the right tool; ambiguity is asked",
  builder: "Owner's words → valid automations, incl. the demo sentence",
  learning: "One owner decision → the rule they follow (ADR-015)",
};

function SuiteRow({ r }: { r: EvalRun }) {
  const [open, setOpen] = useState(false);
  const ok = r.metrics.score >= r.metrics.threshold;
  const m = r.metrics;
  return (
    <li className="border-t border-sand/60 py-2.5 first:border-0 dark:border-twilight">
      <div className="flex flex-wrap items-center gap-3">
        <div className="w-40 shrink-0">
          <div className="font-mono text-sm font-semibold">{r.suite}</div>
          <div className="text-[11px] text-stone">{ABOUT[r.suite]}</div>
        </div>
        <div className="flex min-w-[180px] flex-1 items-center gap-2">
          <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-bone dark:bg-twilight">
            <div className={cx("h-full rounded-full", ok ? "bg-verdant" : "bg-ember")} style={{ width: `${Math.round(m.score * 100)}%` }} />
          </div>
          <span className="w-24 text-right font-mono text-sm">
            {r.passed}/{r.cases} · {Math.round(m.score * 100)}%
          </span>
        </div>
        <Badge tone={ok ? "good" : "bad"}>
          {ok ? "pass" : "fail"} · ≥{Math.round(m.threshold * 100)}%
        </Badge>
        {m.p50_s != null && (
          <span className="text-[11px] text-stone">
            p50 {m.p50_s}s · p95 {m.p95_s}s{m.one_call_turns != null && ` · ${Math.round(Number(m.one_call_turns) * 100)}% one-call`}
          </span>
        )}
        {r.failed.length > 0 && (
          <button className="text-xs text-signal hover:underline" onClick={() => setOpen(!open)}>
            {open ? "hide" : `${r.failed.length} failed`}
          </button>
        )}
      </div>
      {open && (
        <ul className="mt-2 space-y-1 pl-4">
          {r.failed.map((f) => (
            <li key={f.id} className="text-xs">
              <Mono>{f.id}</Mono> <span className="text-dusk dark:text-stone">{f.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function Evals() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["evals"],
    queryFn: () => api.get<Overview>("/evals"),
    refetchInterval: (query) => (query.state.data?.running ? 2000 : 15000),
  });
  const run = useMutation({
    mutationFn: (suite: string) => api.post("/evals/run", {}, { suite }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["evals"] }),
  });
  const d = q.data;
  // latest result per suite, grouped under the build it ran on
  const latest = new Map<string, EvalRun>();
  for (const r of d?.runs ?? []) if (!latest.has(r.suite)) latest.set(r.suite, r);
  const rows = (d?.suites ?? []).map((s) => latest.get(s)).filter((r): r is EvalRun => !!r);
  const builds = [...new Set(rows.map((r) => r.build_ref))];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Evals"
        actions={
          <Button disabled={d?.running || run.isPending} onClick={() => run.mutate("all")}>
            <Play size={14} /> {d?.running ? "Running…" : "Run all suites"}
          </Button>
        }
      >
        The demo build measured with real Gemini on the dedicated eval business — customer turns go through the deployed pipeline, exactly as on a phone.
      </PageHeader>
      <ErrorNote error={q.error ?? run.error} />
      {(d?.running || (d?.log.length ?? 0) > 0) && (
        <Card title={d?.running ? "Running" : "Last run"}>
          <pre className="max-h-48 overflow-auto font-mono text-xs leading-relaxed">{d?.log.join("\n")}</pre>
        </Card>
      )}
      <Card
        title={
          <span>
            Latest results {builds.length > 0 && <span className="font-mono text-xs text-stone">· build {builds.join(", ")}</span>}
          </span>
        }
      >
        {rows.length ? (
          <ul>
            {rows.map((r) => (
              <SuiteRow key={r.id} r={r} />
            ))}
          </ul>
        ) : (
          <Empty>No eval runs yet — run `make eval` or press the button.</Empty>
        )}
        {rows.length > 0 && <div className="mt-3 text-[11px] text-stone">Last run {fmtWhen(rows[0]?.created_at)}</div>}
      </Card>
    </div>
  );
}
