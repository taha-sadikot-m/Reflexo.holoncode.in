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

const SUITE_NAME: Record<string, string> = {
  tool_selection: "Tool selection",
  gate_adversarial: "Output gate (adversarial)",
  grounding: "Grounding",
  language: "Language",
  ask_owner_scope: "Owner answer scope",
  owner_commands: "Owner commands",
  builder: "Automation builder",
  learning: "Learning from decisions",
};

function SuiteRow({ r }: { r: EvalRun }) {
  const [open, setOpen] = useState(false);
  const m = r.metrics;
  const ok = m.score >= m.threshold;
  const pct = Math.round(m.score * 100);
  return (
    <li className="px-5 py-3.5">
      <div className="grid grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_88px_110px_minmax(0,0.9fr)] items-center gap-5">
        <div className="min-w-0">
          <div className="text-[13.5px] font-medium text-ink">{SUITE_NAME[r.suite] ?? r.suite}</div>
          <div className="truncate text-xs text-stone">{ABOUT[r.suite]}</div>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-bone">
          <div className={cx("h-full rounded-full", ok ? "bg-verdant" : "bg-ember")} style={{ width: `${pct}%` }} />
        </div>
        <div className="tabular text-right">
          <div className="text-[13.5px] font-semibold text-ink">{pct}%</div>
          <div className="text-[11px] text-stone">
            {r.passed}/{r.cases} cases
          </div>
        </div>
        <div>
          <Badge tone={ok ? "good" : "bad"} dot>
            {ok ? "Pass" : "Fail"} · ≥{Math.round(m.threshold * 100)}%
          </Badge>
        </div>
        <div className="text-right text-xs text-stone">
          {m.p50_s != null ? (
            <>
              p50 {m.p50_s}s · p95 {m.p95_s}s
            </>
          ) : (
            "in-process"
          )}
          {r.failed.length > 0 && (
            <button className="ml-2 font-medium text-signal hover:text-signal-d" onClick={() => setOpen(!open)}>
              {open ? "Hide" : `${r.failed.length} failed`}
            </button>
          )}
        </div>
      </div>
      {open && (
        <ul className="mt-3 space-y-1 rounded-lg bg-parchment p-3">
          {r.failed.map((f) => (
            <li key={f.id} className="text-xs">
              <Mono>{f.id}</Mono> <span className="text-dusk">{f.detail}</span>
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
          <ul className="-m-5 divide-y divide-sand">
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
