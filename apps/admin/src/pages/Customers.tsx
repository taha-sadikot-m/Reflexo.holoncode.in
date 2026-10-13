import { useQuery } from "@tanstack/react-query";
import { UserRound, Users } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { Field, PageHeader, StatusBadge, fmtWhen, inr } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Skeleton, cx } from "../components/ui";
import { api } from "../lib/api";
import { useSession } from "../lib/session";
import { ChannelIcon, channelLabel } from "../components/channel";

interface CustomerRow {
  id: string;
  name: string | null;
  language: string | null;
  identities: { channel: string; linked_via: string }[];
  orders: number;
  merged: number;
  last_seen_at: string | null;
}

interface CustomerView {
  id: string;
  name: string | null;
  phone: string | null;
  language: string | null;
  tags: string[];
  facts: (string | { text?: string; fact?: string })[];
  summary: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  identities: { channel: string; external_id: string; display_name: string | null; linked_via: string; last_inbound_at: string | null; created_at: string }[];
  merged_from: { id: string; name: string | null; created_at: string }[];
  orders: { id: string; order_number: string; status: string; total_rupees: number | null; channel: string | null; created_at: string }[];
  history: { at: string; direction: string; author: string; text: string; channel: string; conversation_id: string }[];
}

const LINKED: Record<string, string> = {
  first_contact: "first contact",
  link_code: "linked by code",
  payment: "linked by payment",
  same_wa_user: "same WhatsApp user",
  owner_confirmed: "owner confirmed",
};


function Detail({ id, bid }: { id: string; bid: string }) {
  const q = useQuery({
    queryKey: ["customer", bid, id],
    queryFn: () => api.get<CustomerView>(`/customers/${id}`, { business_id: bid }),
    refetchInterval: 5000,
  });
  const c = q.data;
  if (q.error) return <ErrorNote error={q.error} />;
  if (!c) return <Skeleton lines={6} />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-[17px] font-semibold tracking-tight text-ink">{c.name ?? "Unknown customer"}</h2>
        {c.language && <Badge>{c.language}</Badge>}
        {c.tags.map((t) => (
          <Badge key={t} tone="accent">
            {t}
          </Badge>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Field label="Phone">{c.phone}</Field>
        <Field label="First seen">{fmtWhen(c.first_seen_at)}</Field>
        <Field label="Last seen">{fmtWhen(c.last_seen_at)}</Field>
      </div>
      <div>
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">Identities</div>
        <ul className="space-y-1">
          {c.identities.map((i, n) => (
            <li key={n} className="flex flex-wrap items-center gap-2 text-sm">
              <ChannelIcon channel={i.channel} size="sm" />
              <span className="font-medium text-ink">{channelLabel(i.channel)}</span>
              <span className="font-mono text-xs text-dusk dark:text-stone">{i.external_id}</span>
              <Badge tone={i.linked_via === "first_contact" ? "neutral" : "good"}>{LINKED[i.linked_via] ?? i.linked_via}</Badge>
              <span className="text-[11px] text-stone">{fmtWhen(i.created_at)}</span>
            </li>
          ))}
        </ul>
        {c.merged_from.length > 0 && (
          <div className="mt-1 text-xs text-dusk dark:text-stone">
            Merged {c.merged_from.length} profile{c.merged_from.length > 1 ? "s" : ""} into this one — one customer, one history.
          </div>
        )}
      </div>
      {(c.summary || c.facts.length > 0) && (
        <div>
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">What the AI remembers</div>
          {c.summary && <div className="text-sm">{c.summary}</div>}
          <ul className="list-inside list-disc text-sm text-dusk dark:text-stone">
            {c.facts.map((f, n) => (
              <li key={n}>{typeof f === "string" ? f : (f.text ?? f.fact ?? JSON.stringify(f))}</li>
            ))}
          </ul>
        </div>
      )}
      {c.orders.length > 0 && (
        <div>
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">Orders</div>
          {c.orders.map((o) => (
            <Link key={o.id} to={`/orders?o=${o.id}`} className="flex items-center gap-2 py-0.5 text-sm hover:underline">
              <span className="font-mono text-xs">{o.order_number}</span>
              <StatusBadge status={o.status} />
              <span className="font-mono">{inr(o.total_rupees)}</span>
              <span className="text-[11px] text-stone">{fmtWhen(o.created_at)}</span>
            </Link>
          ))}
        </div>
      )}
      <div>
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-stone">History · all channels</div>
        <div className="max-h-[480px] space-y-1.5 overflow-y-auto rounded-lg border border-sand bg-parchment p-3">
          {c.history.length ? (
            c.history.map((h, n) => (
              <div key={n} className={cx("flex", h.direction === "inbound" ? "justify-start" : "justify-end")}>
                <div
                  className={cx(
                    "max-w-[80%] rounded-2xl px-3 py-1.5 text-sm shadow-sm",
                    h.direction === "inbound" ? "rounded-bl-sm bg-white dark:bg-twilight" : "rounded-br-sm bg-signal-l",
                  )}
                >
                  <div className="whitespace-pre-wrap break-words">{h.text}</div>
                  <div className="mt-0.5 flex justify-end gap-1.5 text-[10px] text-stone">
                    <ChannelIcon channel={h.channel} size="sm" className="!h-4 !w-4" />
                    {h.direction !== "inbound" && <span>{h.author}</span>}
                    <span>{fmtWhen(h.at)}</span>
                  </div>
                </div>
              </div>
            ))
          ) : (
            <div className="text-xs text-stone">No messages.</div>
          )}
        </div>
      </div>
    </div>
  );
}

export function Customers() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const [params, setParams] = useSearchParams();
  const selected = params.get("c");
  const list = useQuery({
    queryKey: ["customers", bid],
    queryFn: () => api.get<CustomerRow[]>("/customers", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  return (
    <div className="space-y-5">
      <PageHeader title="Customers">
        One customer across WhatsApp, Messenger, Instagram and Telegram. A “continue on WhatsApp” code proves it is the same person and merges their
        history.
      </PageHeader>
      <ErrorNote error={list.error} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[380px_1fr]">
        <Card>
          {list.data?.length ? (
            <ul className="space-y-0.5">
              {list.data.map((c) => (
                <li key={c.id}>
                  <button
                    onClick={() => setParams({ c: c.id }, { replace: true })}
                    className={cx("w-full rounded-lg px-3 py-2 text-left hover:bg-bone dark:hover:bg-twilight", selected === c.id && "bg-signal-l dark:bg-[#26214d]")}
                  >
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate text-sm font-semibold">{c.name ?? "Unknown"}</span>
                      <span className="flex gap-1">
                        {[...new Set(c.identities.map((i) => i.channel))].map((ch) => (
                          <ChannelIcon key={ch} channel={ch} size="sm" />
                        ))}
                      </span>
                    </div>
                    <div className="mt-0.5 flex gap-3 text-[11px] text-stone">
                      <span>{c.orders} orders</span>
                      {c.merged > 0 && <span className="text-[#00785a] dark:text-verdant">linked</span>}
                      <span className="ml-auto">{fmtWhen(c.last_seen_at)}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty icon={Users} hint="Customers appear when they first message the shop on any channel.">No customers yet</Empty>
          )}
        </Card>
        <Card>{selected ? <Detail id={selected} bid={bid} /> : <Empty icon={UserRound}>Select a customer to see their identities, orders and history</Empty>}</Card>
      </div>
    </div>
  );
}
