import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Badge, Card, Empty, ErrorNote, Mono } from "../components/ui";
import { api, type Health, type TraceSummary } from "../lib/api";
import { useSession } from "../lib/session";
import { CHANNEL_ICON, OUTCOME_LABEL, fmtMs, fmtTime } from "../lib/stages";

const toneFor = (v: string) => (v === "ok" ? "good" : v === "degraded" ? "warn" : "bad");

export function Overview() {
  const { business } = useSession();
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.get<Health>("/health"), refetchInterval: 5000 });
  const traces = useQuery({
    queryKey: ["traces", business?.id, "overview"],
    queryFn: () => api.get<{ items: TraceSummary[] }>("/traces", { business_id: business!.id, limit: "10" }),
    enabled: !!business,
    refetchInterval: 5000,
  });
  const h = health.data;

  return (
    <div className="grid grid-cols-3 gap-5">
      <Card title="Health" className="col-span-1">
        <ErrorNote error={health.error} />
        {h && (
          <div className="space-y-3 text-sm">
            <div className="flex items-center justify-between">
              Postgres <Badge tone={toneFor(h.postgres)}>{h.postgres}</Badge>
            </div>
            <div className="flex items-center justify-between">
              Redis <Badge tone={toneFor(h.redis)}>{h.redis}</Badge>
            </div>
            <div>
              <div className="mb-1 text-xs font-semibold text-dusk">Workers</div>
              {h.workers.length ? (
                h.workers.map((w) => (
                  <div key={w.id} className="flex items-center justify-between py-0.5">
                    <Mono>{w.id.split(":").slice(0, 2).join(":")}</Mono>
                    <Badge tone={w.alive ? "good" : "bad"}>{w.alive ? `alive · ${w.age_s}s` : `stale · ${Math.round(w.age_s)}s`}</Badge>
                  </div>
                ))
              ) : (
                <div className="text-xs text-ember">No worker heartbeat — start the worker.</div>
              )}
            </div>
          </div>
        )}
      </Card>

      <Card title="Queues" className="col-span-2">
        {h && (
          <div className="grid grid-cols-4 gap-3">
            {h.queues.map((q) => (
              <div key={q.pool} className="rounded-lg bg-parchment p-3 dark:bg-ink">
                <div className="text-xs font-semibold uppercase tracking-wide text-dusk">{q.pool}</div>
                <div className="font-display text-2xl font-bold">{q.depth}</div>
                <div className="text-xs text-stone">oldest {q.depth ? `${q.oldest_s}s` : "—"}</div>
              </div>
            ))}
            <div className="col-span-4 flex gap-4 text-xs text-dusk">
              <span>turns running: {h.turns_running ?? 0}</span>
              <span className={h.dead_letters ? "font-semibold text-ember" : ""}>dead letters: {h.dead_letters ?? 0}</span>
            </div>
          </div>
        )}
      </Card>

      <Card title="Latest traces" className="col-span-3" actions={<Link className="text-xs font-semibold text-signal hover:underline" to="/xray">Open X-ray →</Link>}>
        {traces.data?.items.length ? (
          <table className="w-full text-sm">
            <tbody>
              {traces.data.items.map((t) => (
                <tr key={t.trace_id} className="border-t border-sand first:border-0 dark:border-twilight">
                  <td className="w-8 py-2">{CHANNEL_ICON[t.provider ?? ""] ?? "•"}</td>
                  <td className="max-w-md truncate py-2">
                    <Link to={`/xray?trace=${t.trace_id}`} className="hover:underline">
                      {t.snippet ?? t.trace_id.slice(0, 16)}
                    </Link>
                  </td>
                  <td className="py-2">{t.outcome && <Badge>{OUTCOME_LABEL[t.outcome] ?? t.outcome}</Badge>}</td>
                  <td className="py-2 text-right font-mono text-xs">{fmtMs(t.total_ms)}</td>
                  <td className="py-2 text-right text-xs text-stone">{fmtTime(t.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No traces yet.</Empty>
        )}
      </Card>
    </div>
  );
}
