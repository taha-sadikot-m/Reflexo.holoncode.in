import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { Field, PageHeader, StatusBadge, fmtDate, fmtWhen, inr } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type CapacityRow, type OrderDetail, type OrderRow } from "../lib/api";
import { useSession } from "../lib/session";

const FILTERS = ["", "awaiting_payment", "confirmed", "preparing", "out_for_delivery", "delivered", "cancelled", "draft"];

function Capacity({ rows }: { rows: CapacityRow[] }) {
  if (!rows.length) return null;
  return (
    <Card title="Daily capacity">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        {rows.map((r) => {
          const [name, day] = r.key.includes(":") ? r.key.split(":", 2) : [r.key, ""];
          const pct = r.limit ? Math.min(100, (r.used / r.limit) * 100) : 0;
          return (
            <div key={r.key} title={r.automation} className="min-w-0">
              <div className="flex items-baseline justify-between text-xs">
                <span className="font-semibold">{day ? fmtDate(day) : name}</span>
                <Mono>
                  {r.used}/{r.limit}
                </Mono>
              </div>
              <div className="mt-1 h-2 overflow-hidden rounded-full bg-bone dark:bg-twilight">
                <div
                  className={cx("h-full rounded-full", pct >= 100 ? "bg-ember" : pct >= 75 ? "bg-sun" : "bg-verdant")}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <div className="mt-0.5 truncate text-[10px] text-stone">{name}</div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function Detail({ id, bid }: { id: string; bid: string }) {
  const q = useQuery({
    queryKey: ["order", bid, id],
    queryFn: () => api.get<OrderDetail>(`/orders/${id}`, { business_id: bid }),
    refetchInterval: 5000,
  });
  const o = q.data;
  if (q.error) return <ErrorNote error={q.error} />;
  if (!o) return <Empty>Loading…</Empty>;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-[17px] font-semibold tracking-tight text-ink">{o.order_number}</h2>
        <StatusBadge status={o.status} />
        <StatusBadge status={o.payment_status} />
        {o.conversation_id && (
          <Link to={`/conversations?c=${o.conversation_id}`} className="ml-auto inline-flex items-center gap-1 text-xs font-semibold text-signal hover:underline">
            Conversation <ExternalLink size={12} />
          </Link>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Field label="Customer">{o.customer}</Field>
        <Field label="Delivery">
          {fmtDate(o.delivery_date)} {o.delivery_slot && `· ${o.delivery_slot}`}
        </Field>
        <Field label="Payment">{o.payment_method?.toUpperCase()}</Field>
        <Field label="Address">{o.address}</Field>
        <Field label="Placed">{fmtWhen(o.created_at)}</Field>
        <Field label="Confirmed">{fmtWhen(o.confirmed_at)}</Field>
      </div>
      {o.cancelled && (
        <div className="rounded-lg bg-ember-l px-3 py-2 text-sm text-ember dark:bg-[#40201c]">
          Cancelled {fmtWhen(o.cancelled.at)} by {o.cancelled.by ?? "—"}
          {o.cancelled.reason && ` — “${o.cancelled.reason}”`}
        </div>
      )}
      <table className="w-full text-sm">
        <tbody>
          {o.items.map((i, n) => (
            <tr key={n} className="border-b border-sand/60 dark:border-twilight">
              <td className="py-1.5">
                {i.quantity}× {i.name}
                {Object.keys(i.details).length > 0 && (
                  <div className="text-xs text-dusk dark:text-stone">
                    {Object.entries(i.details)
                      .map(([k, v]) => `${k}: ${String(v)}`)
                      .join(" · ")}
                  </div>
                )}
              </td>
              <td className="py-1.5 text-right font-mono">{inr(i.line_rupees)}</td>
            </tr>
          ))}
          {!!o.delivery_fee_rupees && (
            <tr>
              <td className="py-1 text-dusk">Delivery</td>
              <td className="py-1 text-right font-mono">{inr(o.delivery_fee_rupees)}</td>
            </tr>
          )}
          <tr className="font-semibold">
            <td className="py-1.5">Total</td>
            <td className="py-1.5 text-right font-mono">{inr(o.total_rupees)}</td>
          </tr>
        </tbody>
      </table>
      {Object.keys(o.details).length > 0 && (
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(o.details).map(([k, v]) => (
            <Field key={k} label={k.replace(/_/g, " ")}>
              {typeof v === "object" ? JSON.stringify(v) : String(v)}
            </Field>
          ))}
        </div>
      )}
      {o.missing_details.length > 0 && <div className="text-xs text-sun">Still missing: {o.missing_details.join(", ")}</div>}
      {o.payment_links.length > 0 && (
        <div>
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">Payment links</div>
          {o.payment_links.map((p) => (
            <div key={p.id} className="flex items-center gap-2 text-sm">
              <StatusBadge status={p.status} />
              <span className="font-mono">{inr(p.amount_rupees)}</span>
              <span className="text-xs text-stone">{p.paid_at ? `paid ${fmtWhen(p.paid_at)}` : `created ${fmtWhen(p.created_at)}`}</span>
            </div>
          ))}
        </div>
      )}
      {o.proposals.length > 0 && (
        <div>
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">Asked the owner</div>
          {o.proposals.map((p) => (
            <div key={p.id} className="flex flex-wrap items-center gap-2 text-sm">
              <Mono>{p.capability}</Mono>
              <StatusBadge status={p.status} />
              {p.outcome?.learned && p.outcome.learned !== "once" && <Badge tone="accent">learned a rule</Badge>}
              <Link to="/playbook" className="text-xs text-signal hover:underline">
                Playbook →
              </Link>
            </div>
          ))}
        </div>
      )}
      <div>
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">Timeline</div>
        {o.timeline.length ? (
          <ol className="space-y-1 border-l border-sand pl-3 dark:border-twilight">
            {o.timeline.map((e, n) => (
              <li key={n} className="text-sm">
                <Mono>{e.type}</Mono> <span className="text-xs text-stone">· {fmtWhen(e.at)} · {e.source}</span>
              </li>
            ))}
          </ol>
        ) : (
          <div className="text-xs text-stone">No events yet.</div>
        )}
      </div>
    </div>
  );
}

export function Orders() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const selected = params.get("o");
  const list = useQuery({
    queryKey: ["orders", bid, status],
    queryFn: () => api.get<{ items: OrderRow[]; counts: Record<string, number> }>("/orders", { business_id: bid, status }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  const cap = useQuery({
    queryKey: ["capacity", bid],
    queryFn: () => api.get<CapacityRow[]>("/orders/capacity", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 10000,
  });
  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const counts = list.data?.counts ?? {};

  return (
    <div className="space-y-5">
      <PageHeader title="Orders">Every order the AI took, its payment and delivery, and what happened to it — including what the owner decided.</PageHeader>
      <Capacity rows={cap.data ?? []} />
      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f || "all"}
            onClick={() => set("status", f || null)}
            className={cx(
              "rounded-full px-3 py-1 text-xs font-semibold",
              status === f ? "bg-ink text-bone dark:bg-bone dark:text-ink" : "bg-bone text-dusk hover:bg-sand dark:bg-twilight dark:text-sand",
            )}
          >
            {f ? f.replace(/_/g, " ") : "all"} {f && counts[f] ? `· ${counts[f]}` : ""}
          </button>
        ))}
      </div>
      <ErrorNote error={list.error} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[1fr_440px]">
        <Card className="overflow-x-auto">
          {list.data?.items.length ? (
            <table className="w-full min-w-[720px] text-sm">
              <thead className="text-left text-[10px] uppercase tracking-wide text-stone">
                <tr>
                  <th className="pb-2">Order</th>
                  <th className="pb-2">Customer</th>
                  <th className="pb-2">Items</th>
                  <th className="pb-2 text-right">Total</th>
                  <th className="pb-2">Payment</th>
                  <th className="pb-2">Delivery</th>
                  <th className="pb-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((o) => (
                  <tr
                    key={o.id}
                    onClick={() => set("o", o.id)}
                    className={cx(
                      "cursor-pointer border-t border-sand/60 hover:bg-bone dark:border-twilight dark:hover:bg-twilight",
                      selected === o.id && "bg-signal-l dark:bg-[#26214d]",
                    )}
                  >
                    <td className="py-2 font-mono text-xs">
                      {o.order_number}
                      {o.simulated && <span title="Simulated (Lab)"> 🧪</span>}
                    </td>
                    <td className="py-2">{o.customer ?? "—"}</td>
                    <td className="max-w-[220px] truncate py-2 text-dusk dark:text-stone">{o.items}</td>
                    <td className="py-2 text-right font-mono">{inr(o.total_rupees)}</td>
                    <td className="py-2">
                      <span className="text-xs uppercase text-dusk">{o.payment_method ?? ""}</span> <StatusBadge status={o.payment_status} />
                    </td>
                    <td className="py-2 text-xs">
                      {fmtDate(o.delivery_date)}
                      <div className="text-stone">{o.delivery_slot}</div>
                    </td>
                    <td className="py-2">
                      <StatusBadge status={o.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No orders{status ? ` with status ${status.replace(/_/g, " ")}` : ""}.</Empty>
          )}
        </Card>
        <Card>{selected ? <Detail id={selected} bid={bid} /> : <Empty>Pick an order to see its details and timeline.</Empty>}</Card>
      </div>
    </div>
  );
}
