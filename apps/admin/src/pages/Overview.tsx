import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CircleCheck, CircleHelp, IndianRupee, MessageSquareWarning, Receipt, ServerCog, Workflow } from "lucide-react";
import { Link } from "react-router-dom";

import { StatusBadge, fmtWhen, inr } from "../components/bits";
import { ChannelIcon } from "../components/channel";
import { Badge, Card, Empty, ErrorNote, Skeleton, Stat, cx, td, th, tr } from "../components/ui";
import { api, type AutomationRow, type ConversationItem, type Health, type OrderRow, type OwnerWorkspace, type TraceSummary } from "../lib/api";
import { useSession } from "../lib/session";
import { OUTCOME_LABEL, fmtMs } from "../lib/stages";

const LIVE_S = 300; // a worker silent for longer is a past process, not a live one

const isToday = (iso: string | null | undefined) => !!iso && new Date(iso).toDateString() === new Date().toDateString();

function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

function ViewAll({ to }: { to: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-[12.5px] font-medium text-signal hover:text-signal-d">
      View all <ArrowRight size={13} />
    </Link>
  );
}

export function Overview() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const p = { business_id: bid };
  const on = { enabled: !!bid, refetchInterval: 5000 };
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.get<Health>("/health"), refetchInterval: 5000 });
  const orders = useQuery({ queryKey: ["orders", bid, ""], queryFn: () => api.get<{ items: OrderRow[]; counts: Record<string, number> }>("/orders", p), ...on });
  const owner = useQuery({ queryKey: ["owner", bid], queryFn: () => api.get<OwnerWorkspace>("/owner", p), ...on });
  const convs = useQuery({ queryKey: ["conversations", bid], queryFn: () => api.get<{ items: ConversationItem[] }>("/conversations", p), ...on });
  const autos = useQuery({ queryKey: ["automations", bid], queryFn: () => api.get<AutomationRow[]>("/automations", p), ...on });
  const traces = useQuery({
    queryKey: ["traces", bid, "overview"],
    queryFn: () => api.get<{ items: TraceSummary[] }>("/traces", { ...p, limit: "8" }),
    ...on,
  });

  const real = (orders.data?.items ?? []).filter((o) => !o.simulated && o.status !== "draft");
  const today = real.filter((o) => isToday(o.created_at));
  const revenue = today.filter((o) => o.payment_status === "paid").reduce((sum, o) => sum + (o.total_rupees ?? 0), 0);
  const openQuestions = (owner.data?.questions ?? []).filter((q) => q.status === "open" || q.status === "asked");
  const pendingApprovals = (owner.data?.approvals ?? []).filter((a) => a.status === "pending");
  const attention = (convs.data?.items ?? []).filter((c) => c.needs_attention || c.human_active);
  const activeAutos = (autos.data ?? []).filter((a) => a.status === "active");
  const h = health.data;
  const workers = (h?.workers ?? []).filter((w) => w.age_s < LIVE_S);
  const allUp = h && h.postgres === "ok" && workers.length > 0 && workers.every((w) => w.alive);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight text-ink">
          {greeting()}
          {business ? `, ${business.name}` : ""}
        </h1>
        <p className="mt-1 text-[13.5px] text-dusk">Here is what your AI employee is handling today.</p>
      </div>

      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <Stat label="Orders today" value={orders.data ? today.length : "–"} icon={Receipt} hint={`${real.length} in total`} />
        <Stat label="Paid today" value={orders.data ? inr(revenue) : "–"} icon={IndianRupee} tone="good" hint="Online payments received" />
        <Stat
          label="Waiting on the owner"
          value={owner.data ? openQuestions.length + pendingApprovals.length : "–"}
          icon={CircleHelp}
          tone={openQuestions.length + pendingApprovals.length ? "warn" : "good"}
          hint={`${openQuestions.length} questions · ${pendingApprovals.length} decisions`}
        />
        <Stat
          label="Chats needing a person"
          value={convs.data ? attention.length : "–"}
          icon={MessageSquareWarning}
          tone={attention.length ? "bad" : "good"}
          hint={`${activeAutos.length} automations running`}
        />
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1.6fr_1fr]">
        <Card title="Recent orders" actions={<ViewAll to="/orders" />} flush>
          {!orders.data ? (
            <div className="p-5">
              <Skeleton lines={5} />
            </div>
          ) : real.length ? (
            <table className="w-full text-[13px]">
              <thead>
                <tr>
                  <th className={th}>Order</th>
                  <th className={th}>Customer</th>
                  <th className={cx(th, "text-right")}>Total</th>
                  <th className={th}>Status</th>
                  <th className={cx(th, "text-right")}>Placed</th>
                </tr>
              </thead>
              <tbody>
                {real.slice(0, 6).map((o) => (
                  <tr key={o.id} className={tr}>
                    <td className={td}>
                      <Link to={`/orders?o=${o.id}`} className="font-medium text-ink hover:text-signal">
                        {o.order_number}
                      </Link>
                      <div className="max-w-[220px] truncate text-xs text-stone">{o.items}</div>
                    </td>
                    <td className={cx(td, "text-dusk")}>{o.customer ?? "—"}</td>
                    <td className={cx(td, "tabular text-right font-medium text-ink")}>{inr(o.total_rupees)}</td>
                    <td className={td}>
                      <StatusBadge status={o.status} />
                    </td>
                    <td className={cx(td, "text-right text-xs text-stone")}>{fmtWhen(o.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty icon={Receipt} hint="Orders the AI takes on WhatsApp, Telegram or a call appear here.">
              No orders yet
            </Empty>
          )}
        </Card>

        <Card title="Waiting on the owner" actions={<ViewAll to="/owner" />} flush>
          {!owner.data ? (
            <div className="p-5">
              <Skeleton lines={4} />
            </div>
          ) : openQuestions.length + pendingApprovals.length ? (
            <ul className="divide-y divide-sand">
              {openQuestions.slice(0, 4).map((q) => (
                <li key={q.id} className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <Badge tone="warn" dot>
                      Question {q.code}
                    </Badge>
                    <span className="ml-auto text-xs text-stone">{fmtWhen(q.asked_at)}</span>
                  </div>
                  <div className="mt-1.5 line-clamp-2 text-[13px] text-ink">{q.question}</div>
                  {q.customer && <div className="mt-0.5 text-xs text-stone">{q.customer}</div>}
                </li>
              ))}
              {pendingApprovals.slice(0, 3).map((a) => (
                <li key={a.id} className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <Badge tone="accent" dot>
                      Decision {a.code}
                    </Badge>
                    <span className="ml-auto text-xs text-stone">{fmtWhen(a.created_at)}</span>
                  </div>
                  <div className="mt-1.5 line-clamp-2 text-[13px] text-ink">{a.summary ?? a.kind.replace(/_/g, " ")}</div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty icon={CircleCheck} hint="Questions and decisions the AI needs from the owner show up here.">
              Nothing waiting
            </Empty>
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1.6fr_1fr]">
        <Card title="Live activity" actions={<ViewAll to="/xray" />} flush>
          {!traces.data ? (
            <div className="p-5">
              <Skeleton lines={5} />
            </div>
          ) : traces.data.items.length ? (
            <ul className="divide-y divide-sand">
              {traces.data.items.map((t) => (
                <li key={t.trace_id}>
                  <Link to={`/xray?trace=${t.trace_id}`} className="flex items-center gap-3 px-5 py-2.5 hover:bg-bone/70">
                    <ChannelIcon channel={t.provider} size="sm" />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{t.snippet ?? t.trace_id.slice(0, 16)}</span>
                    {t.outcome && (
                      <Badge tone={t.outcome === "error" ? "bad" : t.outcome === "send" ? "good" : "neutral"}>
                        {OUTCOME_LABEL[t.outcome] ?? t.outcome}
                      </Badge>
                    )}
                    <span className="tabular w-16 text-right font-mono text-xs text-dusk">{fmtMs(t.total_ms)}</span>
                    <span className="w-20 text-right text-xs text-stone">{fmtWhen(t.started_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <Empty icon={Workflow} hint="Every message the AI handles is traced stage by stage.">
              No activity yet
            </Empty>
          )}
        </Card>

        <Card
          title="System health"
          actions={
            h && (
              <Badge tone={allUp ? "good" : "bad"} dot>
                {allUp ? "All systems running" : "Attention needed"}
              </Badge>
            )
          }
        >
          <ErrorNote error={health.error} />
          {!h ? (
            <Skeleton lines={5} />
          ) : (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-2">
                {[
                  ["Database", h.postgres],
                  ["Cache", h.redis],
                ].map(([label, state]) => (
                  <div key={label} className="flex items-center justify-between rounded-lg border border-sand px-3 py-2">
                    <span className="text-[13px] text-dusk">{label}</span>
                    <Badge tone={state === "ok" ? "good" : state === "degraded" ? "warn" : "bad"} dot>
                      {state === "ok" ? "OK" : state}
                    </Badge>
                  </div>
                ))}
              </div>
              <div>
                <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-stone">
                  <ServerCog size={13} /> Services
                </div>
                {workers.length ? (
                  <ul className="space-y-1">
                    {workers.map((w) => (
                      <li key={w.id} className="flex items-center justify-between text-[13px]">
                        <span className="text-ink">{w.service.replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase())}</span>
                        <Badge tone={w.alive ? "good" : "warn"} dot>
                          {w.alive ? "Running" : `Silent ${Math.round(w.age_s)}s`}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-[13px] text-ember">No live worker — start the worker processes.</div>
                )}
              </div>
              <div className="grid grid-cols-4 gap-2 border-t border-sand pt-3">
                {h.queues.map((q) => (
                  <div key={q.pool} className="text-center">
                    <div className="tabular text-lg font-semibold text-ink">{q.depth}</div>
                    <div className="text-[11px] capitalize text-stone">{q.pool}</div>
                  </div>
                ))}
              </div>
              {!!h.dead_letters && (
                <div className="text-xs text-ember">
                  {h.dead_letters} failed background job{h.dead_letters > 1 ? "s" : ""} need a look
                </div>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
