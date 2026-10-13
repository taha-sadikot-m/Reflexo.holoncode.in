import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Radio } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Badge, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type TraceDetail, type TraceEvent, type TraceSummary, type XrayEvent } from "../lib/api";
import { useSession } from "../lib/session";
import { ChannelIcon } from "../components/channel";
import { OUTCOME_LABEL, familyOf, fmtMs, fmtTime, stageColor, type StageFamily } from "../lib/stages";
import { useXrayStream, type StreamMode } from "../lib/stream";

function ModeBadge({ mode }: { mode: StreamMode }) {
  if (mode === "live")
    return (
      <Badge tone="good">
        <Radio size={11} /> live
      </Badge>
    );
  if (mode === "polling") return <Badge tone="warn" title="Redis is down; the stream falls back to Postgres polling">Redis down: polling</Badge>;
  if (mode === "offline") return <Badge tone="bad">reconnecting…</Badge>;
  return <Badge>connecting…</Badge>;
}

function outcomeTone(outcome: string | null) {
  if (outcome === "send") return "good" as const;
  if (outcome === "error" || outcome === "fallback") return "bad" as const;
  if (outcome === "ask_owner" || outcome === "handoff") return "warn" as const;
  return "neutral" as const;
}

// ---------------------------------------------------------------- live events → waterfall rows

function toRows(events: (TraceEvent | XrayEvent)[]): TraceEvent[] {
  const withTimes = events.map((e) => {
    const end = e.at ? new Date(e.at).getTime() : 0;
    return { e, start: end - (e.duration_ms ?? 0) };
  });
  const t0 = Math.min(...withTimes.map((x) => x.start));
  return withTimes
    .map(({ e, start }) => ({
      id: e.id,
      stage: e.stage,
      status: e.status,
      start_ms: Math.max(0, start - t0),
      duration_ms: e.duration_ms,
      at: e.at,
      turn_id: e.turn_id,
      attrs: e.attrs,
    }))
    .sort((a, b) => a.start_ms - b.start_ms || (a.at ?? "").localeCompare(b.at ?? ""));
}

function Waterfall({ rows, selected, onSelect }: { rows: TraceEvent[]; selected: string | null; onSelect: (id: string) => void }) {
  const total = Math.max(1, ...rows.map((r) => r.start_ms + (r.duration_ms ?? 0)));
  return (
    <div className="space-y-1">
      {rows.map((r) => {
        const left = (r.start_ms / total) * 100;
        const width = Math.max(0.6, ((r.duration_ms ?? 0) / total) * 100);
        return (
          <button
            key={r.id}
            onClick={() => onSelect(r.id)}
            className={cx("grid w-full grid-cols-[150px_1fr_70px] items-center gap-3 rounded px-1 py-0.5 text-left text-xs hover:bg-bone dark:hover:bg-twilight", selected === r.id && "bg-bone dark:bg-twilight")}
          >
            <span className="truncate font-mono">
              {r.stage}
              {typeof r.attrs.tool === "string" ? `:${r.attrs.tool}` : ""}
            </span>
            <span className="relative h-4 rounded bg-parchment dark:bg-ink">
              <span className="absolute top-0 h-4 rounded" style={{ left: `${left}%`, width: `${width}%`, background: stageColor(r.stage, r.status) }} />
            </span>
            <span className="text-right font-mono text-dusk dark:text-stone">{fmtMs(r.duration_ms)}</span>
          </button>
        );
      })}
    </div>
  );
}

const ARCH: { family: StageFamily; label: string }[] = [
  { family: "ingress", label: "Webhook → Inbox" },
  { family: "identity", label: "Identity" },
  { family: "automation", label: "Automations" },
  { family: "agent", label: "Agent (LangGraph)" },
  { family: "tool", label: "Tools" },
  { family: "gate", label: "Output gate" },
  { family: "outbound", label: "Outbound" },
];

function ArchStrip({ rows }: { rows: TraceEvent[] }) {
  const hit = new Set(rows.map((r) => familyOf(r.stage)));
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {ARCH.map((n, i) => (
        <div key={n.family} className="flex items-center gap-1.5">
          <span
            className={cx("rounded-md border px-2 py-1 text-[11px] font-semibold transition", hit.has(n.family) ? "text-white shadow" : "border-sand text-stone dark:border-twilight")}
            style={hit.has(n.family) ? { background: `var(--color-st-${n.family})`, borderColor: "transparent" } : undefined}
          >
            {n.label}
          </span>
          {i < ARCH.length - 1 && <span className="text-stone">→</span>}
        </div>
      ))}
    </div>
  );
}

function StageCard({ row }: { row: TraceEvent }) {
  return (
    <div className="rounded-lg border border-sand p-3 dark:border-twilight">
      <div className="mb-2 flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: stageColor(row.stage, row.status) }} />
        <span className="font-mono text-sm font-semibold">{row.stage}</span>
        <Badge tone={row.status === "error" ? "bad" : row.status === "skip" ? "neutral" : "good"}>{row.status}</Badge>
        <span className="ml-auto font-mono text-xs text-dusk">{fmtMs(row.duration_ms)}</span>
      </div>
      <table className="w-full text-xs">
        <tbody>
          {Object.entries(row.attrs).map(([k, v]) => (
            <tr key={k} className="align-top">
              <td className="w-40 py-0.5 pr-3 font-mono text-dusk dark:text-stone">{k}</td>
              <td className="break-all py-0.5 font-mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- page

export function Xray() {
  const { business, me } = useSession();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const traceId = params.get("trace");
  const [selectedEvent, setSelectedEvent] = useState<string | null>(null);
  const { events, mode } = useXrayStream(business?.id);

  const list = useQuery({
    queryKey: ["traces", business?.id],
    queryFn: () => api.get<{ items: TraceSummary[] }>("/traces", { business_id: business!.id, limit: "60" }),
    enabled: !!business,
  });
  const detail = useQuery({
    queryKey: ["trace", business?.id, traceId],
    queryFn: () => api.get<TraceDetail>(`/traces/${traceId}`, { business_id: business!.id }),
    enabled: !!business && !!traceId,
  });

  // new stream events refresh the list (and the open trace), debounced
  useEffect(() => {
    if (!events.length) return;
    const t = window.setTimeout(() => {
      void qc.invalidateQueries({ queryKey: ["traces", business?.id] });
      if (traceId && events.some((e) => e.trace_id === traceId)) void qc.invalidateQueries({ queryKey: ["trace", business?.id, traceId] });
    }, 400);
    return () => window.clearTimeout(t);
  }, [events, qc, business?.id, traceId]);

  const traces = list.data?.items ?? [];
  useEffect(() => {
    if (!traceId && traces[0]) setParams({ trace: traces[0].trace_id }, { replace: true });
  }, [traceId, traces, setParams]);

  const rows = useMemo(() => {
    const live = events.filter((e) => e.trace_id === traceId);
    const server = detail.data?.events ?? [];
    const byId = new Map<string, TraceEvent | XrayEvent>();
    for (const e of [...server, ...live]) byId.set(e.id, e);
    return byId.size ? toRows([...byId.values()]) : [];
  }, [events, detail.data, traceId]);
  const selectedRow = rows.find((r) => r.id === selectedEvent) ?? rows.find((r) => r.stage === "gate") ?? rows[rows.length - 1];
  const summary = detail.data?.summary ?? traces.find((t) => t.trace_id === traceId);

  return (
    <div className="grid h-full grid-cols-[380px_1fr] gap-5">
      <Card title="Traces" actions={<ModeBadge mode={mode} />} className="flex min-h-0 flex-col overflow-hidden">
        <ErrorNote error={list.error} />
        {!traces.length && <Empty>No traces yet — send a message on any channel.</Empty>}
        <ul className="-mx-4 -my-4 max-h-[calc(100vh-180px)] divide-y divide-sand overflow-auto dark:divide-twilight">
          {traces.map((t) => (
            <li key={t.trace_id}>
              <button
                onClick={() => {
                  setParams({ trace: t.trace_id });
                  setSelectedEvent(null);
                }}
                className={cx("w-full px-4 py-2.5 text-left hover:bg-parchment dark:hover:bg-ink", t.trace_id === traceId && "bg-blossom dark:bg-twilight")}
              >
                <div className="flex items-center gap-2 text-sm">
                  <ChannelIcon channel={t.provider} size="sm" />
                  <span className="min-w-0 flex-1 truncate">{t.snippet ?? <Mono>{t.trace_id.slice(0, 12)}…</Mono>}</span>
                  <span className="font-mono text-xs text-dusk">{fmtMs(t.total_ms)}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1">
                  {t.outcome && <Badge tone={outcomeTone(t.outcome)}>{OUTCOME_LABEL[t.outcome] ?? t.outcome}</Badge>}
                  {t.model && <Badge tone="accent">{t.model.replace("gemini-", "")}</Badge>}
                  {t.hedged && <Badge tone="warn">hedged</Badge>}
                  {t.gate === false && <Badge tone="bad">gate ✗</Badge>}
                  {!!t.gate_repairs && t.gate !== false && <Badge tone="warn" title="the output gate caught a problem and the model fixed it">gate repaired</Badge>}
                  {t.simulated && <Badge tone="info">simulated</Badge>}
                  {t.errors > 0 && <Badge tone="bad">{t.errors} err</Badge>}
                  <span className="ml-auto text-[11px] text-stone">{fmtTime(t.started_at)}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </Card>

      <div className="flex min-w-0 flex-col gap-5">
        {!traceId ? (
          <Card>
            <Empty>Select a trace.</Empty>
          </Card>
        ) : (
          <>
            <Card
              title={
                <span className="flex items-center gap-2">
                  Trace <Mono>{traceId}</Mono>
                </span>
              }
              actions={
                <>
                  {summary?.conversation_id && (
                    <Link className="text-xs font-semibold text-signal hover:underline" to={`/conversations?c=${summary.conversation_id}`}>
                      Open conversation
                    </Link>
                  )}
                  <a className="flex items-center gap-1 text-xs font-semibold text-signal hover:underline" href={`${me?.links.jaeger ?? "/jaeger"}/trace/${traceId}`} target="_blank" rel="noreferrer">
                    Open in Jaeger <ExternalLink size={12} />
                  </a>
                </>
              }
            >
              <div className="mb-4">
                <ArchStrip rows={rows} />
              </div>
              {rows.length ? <Waterfall rows={rows} selected={selectedRow?.id ?? null} onSelect={setSelectedEvent} /> : <Empty>Loading…</Empty>}
              {summary && (
                <div className="mt-3 flex flex-wrap gap-4 border-t border-sand pt-3 text-xs text-dusk dark:border-twilight dark:text-stone">
                  <span>
                    total <b className="font-mono text-soot dark:text-bone">{fmtMs(summary.total_ms)}</b>
                  </span>
                  <span>stages {summary.stages}</span>
                  {summary.model && <span>model {summary.model}</span>}
                  {detail.data?.runs[0] && (
                    <span>
                      tokens {String(detail.data.runs[0].input_tokens)} in / {String(detail.data.runs[0].output_tokens)} out · {String(detail.data.runs[0].model_calls)} model call(s)
                    </span>
                  )}
                </div>
              )}
            </Card>
            <div className="grid grid-cols-2 gap-5">
              <Card title="Stage">{selectedRow ? <StageCard row={selectedRow} /> : <Empty>—</Empty>}</Card>
              <Card title="Outbound parts">
                {detail.data?.outbound.length ? (
                  <table className="w-full text-xs">
                    <thead className="text-left text-dusk">
                      <tr>
                        <th className="py-1">#</th>
                        <th>status</th>
                        <th>provider id</th>
                        <th>tries</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.outbound.map((p) => (
                        <tr key={p.id} className="border-t border-sand dark:border-twilight">
                          <td className="py-1 font-mono">{p.part}</td>
                          <td>
                            <Badge tone={p.status === "sent" ? "good" : p.status === "failed" ? "bad" : "neutral"}>{p.status}</Badge>
                          </td>
                          <td className="font-mono">{p.provider_message_id ?? "—"}</td>
                          <td className="font-mono">{p.attempts}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <Empty>No outbound for this trace.</Empty>
                )}
              </Card>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
