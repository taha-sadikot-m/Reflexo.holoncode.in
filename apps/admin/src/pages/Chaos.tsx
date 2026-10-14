import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Loader2, Lock, Play, ServerCrash, ShieldAlert, Skull, Timer, Zap, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Badge, Button, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type ChaosOverview, type ChaosRun } from "../lib/api";
import { useSession } from "../lib/session";
import { fmtTime } from "../lib/stages";

const ACTIONS: Record<string, { label: string; icon: LucideIcon }> = {
  duplicate_webhook: { label: "Duplicate webhook", icon: Copy },
  burst: { label: "Message burst", icon: Zap },
  kill_worker: { label: "Kill worker mid-reply", icon: Skull },
  slow_model: { label: "Slow model · 60 s", icon: Timer },
  redis_down: { label: "Redis down · 60 s", icon: ServerCrash },
  prompt_injection: { label: "Prompt injection", icon: ShieldAlert },
  cross_business_read: { label: "Cross-business read", icon: Lock },
};

const FLAG_LABEL: Record<string, string> = {
  slow_model: "Primary model slowed",
  redis_disabled: "Redis disabled",
  kill_after_tool: "Worker kill armed",
};

const HIDDEN_CHECKS = new Set(["actor"]);

function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function CheckValue({ value }: { value: unknown }) {
  if (typeof value === "boolean") return <Badge tone={value ? "good" : "bad"}>{value ? "yes" : "no"}</Badge>;
  if (value === null || value === undefined) return <Mono>-</Mono>;
  if (typeof value === "number") return <span className="font-mono text-xs font-semibold">{value}</span>;
  if (typeof value === "string") return <span className="text-xs">{value}</span>;
  if (typeof value === "object" && "passed" in value) {
    // output gate result: { passed, failed: [...], rewritten_action }
    const gate = value as { passed: boolean; failed?: string[]; rewritten_action?: string | null };
    return (
      <span className="flex flex-wrap items-center gap-1">
        <Badge tone={gate.passed ? "good" : "bad"}>{gate.passed ? "passed" : `failed: ${(gate.failed ?? []).join(", ")}`}</Badge>
        {gate.rewritten_action && <Badge tone="info">rewritten → {gate.rewritten_action.replaceAll("_", " ")}</Badge>}
      </span>
    );
  }
  return <Mono>{JSON.stringify(value)}</Mono>;
}

function Checks({ checks }: { checks: Record<string, unknown> }) {
  const reply = typeof checks.reply === "string" ? checks.reply : null;
  const rows = Object.entries(checks).filter(([k]) => !HIDDEN_CHECKS.has(k) && k !== "reply");
  return (
    <div className="space-y-2">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-xs text-dusk">{k.replaceAll("_", " ")}</dt>
            <dd className="min-w-0 break-words">
              <CheckValue value={v} />
            </dd>
          </div>
        ))}
      </dl>
      {reply && (
        <blockquote className="whitespace-pre-line rounded-lg border-l-4 border-verdant bg-parchment px-3 py-2 text-xs">{reply}</blockquote>
      )}
    </div>
  );
}

function ActionCard({
  kind,
  expected,
  last,
  running,
  busy,
  onRun,
  now,
}: {
  kind: string;
  expected: string;
  last: ChaosRun | undefined;
  running: ChaosRun | undefined;
  busy: boolean;
  onRun: () => void;
  now: number;
}) {
  const meta = ACTIONS[kind] ?? { label: kind, icon: Play };
  const Icon = meta.icon;
  const outcome = last?.outcome;
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Icon size={15} className="text-saffron" />
          {meta.label}
        </span>
      }
      actions={
        running ? (
          <Badge tone="info">
            <Loader2 size={11} className="animate-spin" />
            running · {Math.max(0, Math.round((now - Date.parse(running.at)) / 1000))} s
          </Badge>
        ) : (
          <Button onClick={onRun} disabled={busy} title={busy ? "Another action is running" : undefined}>
            <Play size={13} /> Run
          </Button>
        )
      }
    >
      <p className="mb-3 text-sm text-dusk">
        <span className="font-semibold text-ink">Expected: </span>
        {expected}
      </p>
      {outcome ? (
        <div className="border-t border-sand pt-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-2">
              <Badge tone={outcome.ok ? "good" : "bad"}>{outcome.ok ? "Verified" : "Not as expected"}</Badge>
              <Mono>
                {outcome.seconds} s · {fmtTime(last!.at)}
              </Mono>
            </span>
            {outcome.conversation_id && (
              <Link to={`/inbox?c=${outcome.conversation_id}`} className="text-xs font-semibold text-signal hover:underline">
                Conversation →
              </Link>
            )}
          </div>
          <Checks checks={outcome.checks} />
        </div>
      ) : last?.state === "abandoned" ? (
        <div className="border-t border-sand pt-3 text-xs text-stone">Last run was interrupted (API restarted).</div>
      ) : (
        !running && <div className="border-t border-sand pt-3 text-xs text-stone">Not run yet.</div>
      )}
    </Card>
  );
}

export function Chaos() {
  const { business } = useSession();
  const qc = useQueryClient();
  const now = useNow();
  const key = ["chaos", business?.id];
  const overview = useQuery({
    queryKey: key,
    queryFn: () => api.get<ChaosOverview>("/chaos", { business_id: business!.id }),
    enabled: !!business,
    refetchInterval: (q) => (q.state.data?.recent.some((r) => r.state === "running") ? 1500 : 10000),
  });
  const start = useMutation({
    mutationFn: (kind: string) => api.post<{ id: string }>(`/chaos/${kind}`, {}, { business_id: business!.id }),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });
  const disarm = useMutation({
    mutationFn: (flag: string) => api.post(`/chaos/flags/${flag}/clear`),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });

  const data = overview.data;
  const running = data?.recent.find((r) => r.state === "running");
  const lastByKind = new Map<string, ChaosRun>();
  for (const r of data?.recent ?? []) if (r.state !== "running" && !lastByKind.has(r.kind)) lastByKind.set(r.kind, r);
  const flags = Object.entries(data?.flags ?? {}).filter(([k]) => k in FLAG_LABEL);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight text-ink">Chaos</h1>
          <p className="max-w-3xl text-sm text-dusk">
            Each action breaks something on purpose, using a simulated customer through the real pipeline. The verdict is computed afterwards from
            the database, not assumed. One action runs at a time.
          </p>
        </div>
        {flags.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {flags.map(([k, f]) => {
              const left = f.expires_at ? Math.max(0, Math.round((Date.parse(f.expires_at) - now) / 1000)) : null;
              return (
                <span key={k} className="flex items-center gap-2 rounded-lg border border-sun bg-sun-l px-3 py-1.5 text-xs font-semibold text-[#8a6100]">
                  {FLAG_LABEL[k]}
                  {left !== null && <span className="font-mono">{left} s</span>}
                  <button className="underline hover:no-underline" onClick={() => disarm.mutate(k)} disabled={disarm.isPending}>
                    disarm
                  </button>
                </span>
              );
            })}
          </div>
        )}
      </div>

      <ErrorNote error={overview.error ?? start.error ?? disarm.error} />

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        {(data?.actions ?? []).map((a) => (
          <ActionCard
            key={a.kind}
            kind={a.kind}
            expected={a.expected}
            last={lastByKind.get(a.kind)}
            running={running?.kind === a.kind ? running : undefined}
            busy={!!running || start.isPending}
            onRun={() => start.mutate(a.kind)}
            now={now}
          />
        ))}
      </div>

      <Card title="History">
        {data?.recent.length ? (
          <table className="w-full text-sm">
            <tbody>
              {data.recent.map((r) => (
                <tr key={r.id} className="border-t border-sand first:border-0">
                  <td className="py-1.5 text-xs text-stone">{fmtTime(r.at)}</td>
                  <td className="py-1.5">{ACTIONS[r.kind]?.label ?? r.kind}</td>
                  <td className="py-1.5">
                    {r.state === "running" ? (
                      <Badge tone="info">running</Badge>
                    ) : r.state === "abandoned" ? (
                      <Badge tone="warn">interrupted</Badge>
                    ) : (
                      <Badge tone={r.outcome?.ok ? "good" : "bad"}>{r.outcome?.ok ? "verified" : "failed"}</Badge>
                    )}
                  </td>
                  <td className={cx("py-1.5 text-right font-mono text-xs", !r.outcome && "text-stone")}>{r.outcome ? `${r.outcome.seconds} s` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No chaos runs yet for this business.</Empty>
        )}
      </Card>
    </div>
  );
}
