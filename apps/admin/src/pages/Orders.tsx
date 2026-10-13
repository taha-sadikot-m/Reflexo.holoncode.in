import { useQuery } from "@tanstack/react-query";
import { ExternalLink, FlaskConical, Receipt, Sparkles } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { Field, PageHeader, SectionLabel, StatusBadge, eventLabel, fmtDate, fmtWhen, inr, paymentLabel } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Segmented, Skeleton, cx, td, th, tr } from "../components/ui";
import { api, type CapacityRow, type OrderDetail, type OrderRow } from "../lib/api";
import { useSession } from "../lib/session";

const FILTERS = ["", "awaiting_payment", "confirmed", "preparing", "out_for_delivery", "delivered", "cancelled", "draft"];
const filterLabel = (f: string) => (f ? f.charAt(0).toUpperCase() + f.slice(1).replace(/_/g, " ") : "All");

function Capacity({ rows }: { rows: CapacityRow[] }) {
  if (!rows.length) return null;
  return (
    <Card title="Daily capacity" actions={<span className="text-xs text-stone">Limits set by the owner's automations</span>}>
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4 xl:grid-cols-7">
        {rows.map((r) => {
          const [name, day] = r.key.includes(":") ? r.key.split(":", 2) : [r.key, ""];
          const pct = r.limit ? Math.min(100, (r.used / r.limit) * 100) : 0;
          return (
            <div key={r.key} title={r.automation} className="min-w-0">
              <div className="flex items-baseline justify-between">
                <span className="text-[13px] font-medium text-ink">{day ? fmtDate(day) : name}</span>
                <span className="tabular text-xs text-dusk">
                  {r.used}/{r.limit}
                </span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-bone">
                <div
                  className={cx("h-full rounded-full transition-all", pct >= 100 ? "bg-ember" : pct >= 75 ? "bg-sun" : "bg-signal")}
                  style={{ width: `${Math.max(pct, pct ? 6 : 0)}%` }}
                />
              </div>
              <div className="mt-1 truncate text-[11px] capitalize text-stone">{name}</div>
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
  if (!o) return <Skeleton lines={8} />;
  const details = Object.entries(o.details).filter(([k]) => !["address", "delivery_date", "delivery_slot"].includes(k));
  return (
    <div className="space-y-5">
      <div>
        <div className="flex items-center gap-2">
          <h2 className="text-[18px] font-semibold tracking-tight text-ink">{o.order_number}</h2>
          {o.conversation_id && (
            <Link
              to={`/inbox?c=${o.conversation_id}`}
              className="ml-auto inline-flex items-center gap-1 text-[12.5px] font-medium text-signal hover:text-signal-d"
            >
              Open chat <ExternalLink size={12} />
            </Link>
          )}
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <StatusBadge status={o.status} />
          <StatusBadge status={o.payment_status} />
        </div>
      </div>

      {o.cancelled && (
        <div className="rounded-lg border border-[#f8cdd6] bg-ember-l px-3 py-2.5 text-[13px] text-[#a41034]">
          Cancelled {fmtWhen(o.cancelled.at)} by {o.cancelled.by ?? "—"}
          {o.cancelled.reason && <span className="text-[#a41034]/80"> — “{o.cancelled.reason}”</span>}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        <Field label="Customer">{o.customer}</Field>
        <Field label="Payment">{paymentLabel(o.payment_method)}</Field>
        <Field label="Delivery">
          {fmtDate(o.delivery_date)}
          {o.delivery_slot && <span className="text-dusk"> · {o.delivery_slot}</span>}
        </Field>
        <Field label="Address">{o.address}</Field>
        <Field label="Placed">{fmtWhen(o.created_at)}</Field>
        <Field label="Confirmed">{fmtWhen(o.confirmed_at)}</Field>
      </div>

      <div className="rounded-lg border border-sand">
        {o.items.map((i, n) => (
          <div key={n} className="flex items-start justify-between gap-3 border-b border-sand px-3 py-2.5 last:border-0">
            <div className="min-w-0">
              <div className="text-[13px] text-ink">
                <span className="tabular text-dusk">{i.quantity}×</span> {i.name}
              </div>
              {Object.keys(i.details).length > 0 && (
                <div className="mt-0.5 text-xs text-stone">
                  {Object.entries(i.details)
                    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`)
                    .join(" · ")}
                </div>
              )}
            </div>
            <span className="tabular text-[13px] text-ink">{inr(i.line_rupees)}</span>
          </div>
        ))}
        {!!o.delivery_fee_rupees && (
          <div className="flex justify-between border-t border-sand px-3 py-2 text-[13px] text-dusk">
            <span>Delivery</span>
            <span className="tabular">{inr(o.delivery_fee_rupees)}</span>
          </div>
        )}
        <div className="flex justify-between rounded-b-lg border-t border-sand bg-parchment px-3 py-2.5 text-[13.5px] font-semibold text-ink">
          <span>Total</span>
          <span className="tabular">{inr(o.total_rupees)}</span>
        </div>
      </div>

      {details.length > 0 && (
        <div>
          <SectionLabel>Order details</SectionLabel>
          <div className="grid grid-cols-2 gap-4">
            {details.map(([k, v]) => (
              <Field key={k} label={k.replace(/_/g, " ")}>
                {typeof v === "object" ? JSON.stringify(v) : String(v)}
              </Field>
            ))}
          </div>
        </div>
      )}
      {o.missing_details.length > 0 && (
        <div className="rounded-lg bg-sun-l px-3 py-2 text-xs text-[#8a4b00]">Still missing: {o.missing_details.join(", ")}</div>
      )}

      {o.payment_links.length > 0 && (
        <div>
          <SectionLabel>Payment links</SectionLabel>
          <div className="space-y-1.5">
            {o.payment_links.map((p) => (
              <div key={p.id} className="flex items-center gap-2 text-[13px]">
                <StatusBadge status={p.status} />
                <span className="tabular text-ink">{inr(p.amount_rupees)}</span>
                <span className="ml-auto text-xs text-stone">{p.paid_at ? `paid ${fmtWhen(p.paid_at)}` : `sent ${fmtWhen(p.created_at)}`}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {o.proposals.length > 0 && (
        <div>
          <SectionLabel>Asked the owner</SectionLabel>
          <div className="space-y-1.5">
            {o.proposals.map((p) => (
              <div key={p.id} className="flex flex-wrap items-center gap-2 text-[13px]">
                <span className="text-ink">{eventLabel(p.capability)}</span>
                <StatusBadge status={p.status} />
                {p.outcome?.learned && p.outcome.learned !== "once" && (
                  <Badge tone="accent">
                    <Sparkles size={11} /> Learned a rule
                  </Badge>
                )}
                <Link to="/brain/rules" className="ml-auto text-xs font-medium text-signal hover:text-signal-d">
                  Playbook
                </Link>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <SectionLabel>Timeline</SectionLabel>
        {o.timeline.length ? (
          <ol className="relative space-y-3 pl-5">
            <span className="absolute bottom-1 left-[5px] top-1 w-px bg-sand" />
            {o.timeline.map((e, n) => (
              <li key={n} className="relative">
                <span
                  className={cx(
                    "absolute -left-5 top-1 h-[11px] w-[11px] rounded-full border-2 border-white ring-1",
                    e.type.includes("cancel") ? "bg-ember ring-[#f8cdd6]" : e.type.includes("paid") || e.type.includes("captured") || e.type.includes("confirmed") ? "bg-verdant ring-[#c6ecdb]" : "bg-signal ring-[#d9d6ff]",
                  )}
                />
                <div className="text-[13px] text-ink">{eventLabel(e.type)}</div>
                <div className="text-xs text-stone">
                  {fmtWhen(e.at)} · {e.source}
                </div>
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
  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-6">
      <PageHeader title="Orders">Every order the AI took — its payment, delivery and what happened to it, including what the owner decided.</PageHeader>
      <Capacity rows={cap.data ?? []} />
      <Segmented
        value={status}
        onChange={(v) => set("status", v || null)}
        options={FILTERS.map((f) => ({
          value: f,
          label: (
            <span>
              {filterLabel(f)}
              <span className="tabular ml-1.5 text-stone">{f ? (counts[f] ?? 0) : total}</span>
            </span>
          ),
        }))}
      />
      <ErrorNote error={list.error} />
      <div className="grid grid-cols-1 items-start gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Card flush className="overflow-hidden">
          {!list.data ? (
            <div className="p-5">
              <Skeleton lines={8} />
            </div>
          ) : list.data.items.length ? (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-[13px]">
                <thead className="bg-parchment/60">
                  <tr>
                    <th className={th}>Order</th>
                    <th className={th}>Customer</th>
                    <th className={cx(th, "text-right")}>Total</th>
                    <th className={th}>Payment</th>
                    <th className={th}>Delivery</th>
                    <th className={th}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.items.map((o) => (
                    <tr key={o.id} onClick={() => set("o", o.id)} className={cx(tr, "cursor-pointer", selected === o.id && "bg-signal-l/60 hover:bg-signal-l/60")}>
                      <td className={td}>
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium text-ink">{o.order_number}</span>
                          {o.simulated && (
                            <span title="Created in the Simulation Lab" className="text-stone">
                              <FlaskConical size={12} />
                            </span>
                          )}
                        </div>
                        <div className="max-w-[260px] truncate text-xs text-stone">{o.items}</div>
                      </td>
                      <td className={cx(td, "text-dusk")}>{o.customer ?? "—"}</td>
                      <td className={cx(td, "tabular text-right font-medium text-ink")}>{inr(o.total_rupees)}</td>
                      <td className={td}>
                        <div className="text-[12.5px] text-dusk">{paymentLabel(o.payment_method)}</div>
                        <div className="mt-0.5">
                          <StatusBadge status={o.payment_status} />
                        </div>
                      </td>
                      <td className={td}>
                        <div className="text-[12.5px] text-ink">{fmtDate(o.delivery_date)}</div>
                        <div className="text-xs text-stone">{o.delivery_slot}</div>
                      </td>
                      <td className={td}>
                        <StatusBadge status={o.status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty icon={Receipt} hint="Orders appear as soon as a customer orders on any channel.">
              No {status ? filterLabel(status).toLowerCase() : ""} orders
            </Empty>
          )}
        </Card>
        <Card className="xl:sticky xl:top-0">
          {selected ? (
            <Detail id={selected} bid={bid} />
          ) : (
            <Empty icon={Receipt} hint="Items, payment, delivery and the full timeline.">
              Select an order to see its details
            </Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
