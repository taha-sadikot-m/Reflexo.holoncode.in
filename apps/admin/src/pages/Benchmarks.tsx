// Benchmarks (BETA_12 §8.1, §9.3): each run against the accepted baseline, regressions highlighted; a new baseline is
// accepted only with a reason.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { useState } from "react";

import { PageHeader } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono, Skeleton, cx, td, th, tr } from "../components/ui";
import { api, type BenchMeasure, type BenchResults, type BenchmarksPage } from "../lib/api";
import { useSession } from "../lib/session";
import { micro } from "./Costs";

const LABELS: Record<string, string> = {
  chat_reply_ms: "Chat reply time",
  owner_reply_ms: "Owner reply time",
  cost_per_chat_turn_micro_inr: "Cost per chat turn",
  model_calls_per_turn: "Model calls per turn",
  input_tokens_per_call: "Input tokens per call",
  output_tokens_per_turn: "Output tokens per turn",
  db_queries_per_turn: "Database queries per turn",
  coalesce_wait_ms: "Coalescing wait",
  one_call_turns: "Turns in one call",
  cached_share: "Cached input",
  split_burst_rate: "Split bursts",
};
const SHARES = new Set(["one_call_turns", "cached_share", "split_burst_rate"]);

function value(m: BenchMeasure | undefined) {
  return m ? (m.median ?? m.mean) : null;
}

function show(name: string, v: number | null | undefined) {
  if (v == null) return "-";
  if (name.endsWith("_ms")) return `${(v / 1000).toFixed(2)} s`;
  if (name.endsWith("_micro_inr")) return micro(v);
  if (SHARES.has(name)) return `${Math.round(v * 100)}%`;
  return v.toLocaleString("en-IN", { maximumFractionDigits: 1 });
}

function Delta({ now, base, higherBetter, gated }: { now: number | null; base: number | null; higherBetter: boolean; gated: boolean }) {
  if (now == null || base == null || base === 0) return <span className="text-stone">-</span>;
  const pct = ((now - base) / base) * 100;
  const worse = higherBetter ? pct < 0 : pct > 0;
  const big = Math.abs(pct) >= 1;
  return (
    <span className={cx("font-mono text-xs", big && worse ? (gated ? "font-semibold text-ember" : "text-saffron") : big ? "text-verdant" : "text-stone")}>
      {pct > 0 ? "+" : ""}
      {pct.toFixed(1)}%
    </span>
  );
}

function Verdict({ passed, results }: { passed: boolean | null; results: BenchResults | null }) {
  if (passed === null) return <Badge tone="neutral">not compared</Badge>;
  if (!passed) return <Badge tone="bad">{results?.regressions.length ?? 0} regression(s)</Badge>;
  return <Badge tone="good">passed</Badge>;
}

export function Benchmarks() {
  const { me } = useSession();
  const qc = useQueryClient();
  const page = useQuery({ queryKey: ["benchmarks"], queryFn: () => api.get<BenchmarksPage>("/benchmarks"), refetchInterval: 30000 });
  const [picked, setPicked] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const accept = useMutation({
    mutationFn: () => api.post<{ id: string }>(`/benchmarks/${picked[0]}/accept`, { reason, with: picked.slice(1) }),
    onSuccess: () => {
      setPicked([]);
      setReason("");
      void qc.invalidateQueries({ queryKey: ["benchmarks"] });
    },
  });
  const d = page.data;
  const base = d?.baseline?.results;
  const names = Object.keys(LABELS).filter((n) => base?.measures[n] || d?.runs.some((r) => r.results?.measures[n]));
  const runs = (d?.runs ?? []).filter((r) => !r.is_baseline);
  const higher = new Set(d?.higher_is_better ?? []);

  return (
    <div className="space-y-5">
      <PageHeader title="Benchmarks">
        The benchmark set runs scripted conversations through the real pipeline. A run fails when a gated measure is worse than the baseline by
        more than {d?.allowed_pct ?? 10}% plus its noise band.
      </PageHeader>
      <ErrorNote error={page.error} />
      {!d ? (
        <Skeleton lines={6} />
      ) : (
        <>
          <Card
            title="Accepted baseline"
            actions={
              d.baseline ? (
                <span className="flex items-center gap-2 text-xs text-stone">
                  {base?.runs} run(s) {base?.provisional && <Badge tone="warn">provisional</Badge>} · from the {d.baseline.source}
                </span>
              ) : null
            }
            flush
          >
            {!base ? (
              <Empty>No baseline accepted for set {d.set_version} yet.</Empty>
            ) : (
              <>
                {base.accepted_reason && (
                  <p className="border-b border-sand px-4 py-3 text-xs text-dusk">
                    Accepted by {base.accepted_by}: “{base.accepted_reason}”
                  </p>
                )}
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className={th}>Measure</th>
                      <th className={th}>Median</th>
                      <th className={th}>Slowest 5%</th>
                      <th className={th}>Noise band</th>
                      <th className={th}>Target</th>
                    </tr>
                  </thead>
                  <tbody>
                    {names.map((n) => {
                      const m = base.measures[n];
                      const target = d.targets[n];
                      return (
                        <tr key={n} className={tr}>
                          <td className={td}>
                            {LABELS[n]} {d.gated[n] && <Badge tone="accent">gated</Badge>}
                          </td>
                          <td className={td}>
                            <Mono>{show(n, value(m))}</Mono>
                          </td>
                          <td className={td}>
                            <Mono>{show(n, m?.p95)}</Mono>
                          </td>
                          <td className={td}>
                            <Mono>{base.noise_pct[`${n}.median`] != null ? `${base.noise_pct[`${n}.median`]}%` : "-"}</Mono>
                          </td>
                          <td className={td}>
                            <Mono>{target ? Object.entries(target).map(([stat, t]) => `${stat} ${show(n, t)}`).join(", ") : "-"}</Mono>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </>
            )}
          </Card>

          <Card title="Runs" flush>
            {runs.length === 0 ? (
              <Empty hint="python tasks.py benchmark run">No benchmark runs yet.</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      {!me?.read_only && <th className={th} />}
                      <th className={th}>Run</th>
                      <th className={th}>Verdict</th>
                      {names.map((n) => (
                        <th key={n} className={th}>
                          {LABELS[n]}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => {
                      const regressed = new Set((r.results?.regressions ?? []).map((x) => x.measure.split(".")[0]));
                      return (
                        <tr key={r.id} className={tr}>
                          {!me?.read_only && (
                            <td className={td}>
                              <input
                                type="checkbox"
                                aria-label="Use this run in the new baseline"
                                checked={picked.includes(r.id)}
                                onChange={(e) => setPicked((p) => (e.target.checked ? [...p, r.id] : p.filter((x) => x !== r.id)))}
                                className="accent-signal"
                              />
                            </td>
                          )}
                          <td className={td}>
                            <div className="text-xs text-ink">{new Date(r.started_at).toLocaleString()}</div>
                            <Mono className="text-stone">{r.git_sha.slice(0, 8)}</Mono>
                          </td>
                          <td className={td}>
                            <Verdict passed={r.passed} results={r.results} />
                          </td>
                          {names.map((n) => (
                            <td key={n} className={cx(td, regressed.has(n) && "bg-ember-l/60")}>
                              <div className="font-mono text-xs">{show(n, value(r.results?.measures[n]))}</div>
                              <Delta
                                now={value(r.results?.measures[n])}
                                base={value(base?.measures[n])}
                                higherBetter={higher.has(n)}
                                gated={!!d.gated[n]}
                              />
                            </td>
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {!me?.read_only && (
            <Card title="Accept a new baseline">
              <form
                className="space-y-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  accept.mutate();
                }}
              >
                <p className="text-sm text-dusk">
                  Pick the runs to combine (5 for a full noise band; fewer makes a provisional baseline). Accept only when every measure is equal or
                  better, or when Taha accepts the trade. Write why; also record it in <Mono>_PROGRESS.md</Mono>.
                </p>
                <textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  rows={2}
                  maxLength={500}
                  placeholder="Why this is the new baseline"
                  className="w-full rounded-md p-2 text-[13px] ring-1 ring-inset ring-sand focus:outline-none"
                />
                <div className="flex items-center gap-3">
                  <Button type="submit" disabled={picked.length === 0 || reason.trim().length < 10 || accept.isPending}>
                    <Check size={14} /> Accept {picked.length || ""} run(s) as the baseline
                  </Button>
                  <ErrorNote error={accept.error} />
                </div>
              </form>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
