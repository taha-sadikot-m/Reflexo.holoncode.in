import { useQuery } from "@tanstack/react-query";
import { BookText, Clock3, MapPin, Package, ScrollText, Store, Wallet } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { Field, PageHeader, SectionLabel, fmtWhen, inr } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Segmented, Skeleton, cx, td, th, tr } from "../components/ui";
import { api } from "../lib/api";
import { useSession } from "../lib/session";

interface KnowledgeView {
  catalogue: {
    id: string;
    name: string;
    description: string | null;
    category: string | null;
    status: string;
    price_on_request: boolean;
    base_rupees: number | null;
    variants: { name: string; rupees: number | null; status: string }[];
    aliases: string[];
    attributes: Record<string, unknown>;
    source: string;
    updated_at: string;
  }[];
  qa: {
    id: string;
    question: string;
    answer: string;
    scope: string;
    customer: string | null;
    valid_until: string | null;
    topic: string | null;
    source: string;
    created_at: string;
  }[];
  profile: Record<string, any>;
}

type View = "catalogue" | "qa" | "profile";

const SCOPE: Record<string, { label: string; tone: "good" | "info" | "warn" }> = {
  global: { label: "Everyone", tone: "good" },
  customer: { label: "One customer", tone: "info" },
  temporary: { label: "Temporary", tone: "warn" },
};

const DAY: Record<string, string> = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

function Catalogue({ items }: { items: KnowledgeView["catalogue"] }) {
  if (!items.length) return <Empty icon={Package} hint="The catalogue fills when the owner sends a menu or price list.">No products yet</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-[13px]">
        <thead className="bg-parchment/60">
          <tr>
            <th className={th}>Product</th>
            <th className={th}>Category</th>
            <th className={th}>Sizes &amp; prices</th>
            <th className={th}>Also known as</th>
            <th className={th}>Status</th>
          </tr>
        </thead>
        <tbody>
          {items.map((o) => (
            <tr key={o.id} className={tr}>
              <td className={td}>
                <div className="font-medium text-ink">{o.name}</div>
                {o.description && <div className="max-w-[320px] truncate text-xs text-stone">{o.description}</div>}
              </td>
              <td className={cx(td, "capitalize text-dusk")}>{o.category ?? "-"}</td>
              <td className={td}>
                {o.price_on_request ? (
                  <Badge tone="warn">Price on request</Badge>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {o.variants.length
                      ? o.variants.map((v) => (
                          <span key={v.name} className="tabular rounded-md bg-bone px-1.5 py-0.5 text-[12px] text-ink">
                            {v.name} · {inr(v.rupees)}
                          </span>
                        ))
                      : <span className="tabular text-ink">{inr(o.base_rupees)}</span>}
                  </div>
                )}
              </td>
              <td className={cx(td, "max-w-[220px] truncate text-xs text-stone")}>{o.aliases.join(", ") || "-"}</td>
              <td className={td}>
                <Badge tone={o.status === "active" ? "good" : "neutral"} dot>
                  {o.status === "active" ? "On sale" : "Hidden"}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QA({ items }: { items: KnowledgeView["qa"] }) {
  if (!items.length) return <Empty icon={BookText} hint="Answers the owner gives the AI are saved here and reused.">No saved answers yet</Empty>;
  return (
    <ul className="divide-y divide-sand">
      {items.map((q) => {
        const s = SCOPE[q.scope] ?? { label: q.scope, tone: "info" as const };
        return (
          <li key={q.id} className="px-5 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={s.tone} dot>
                {s.label}
                {q.customer ? ` · ${q.customer}` : ""}
              </Badge>
              {q.valid_until && <Badge tone="warn">Until {fmtWhen(q.valid_until)}</Badge>}
              <span className="text-xs text-stone">
                {q.source === "owner_answer" || q.source === "learning" ? "Learned from the owner" : q.source} · {fmtWhen(q.created_at)}
              </span>
            </div>
            <div className="mt-2 text-[13.5px] font-medium text-ink">{q.question}</div>
            <div className="mt-0.5 text-[13.5px] text-dusk">{q.answer}</div>
          </li>
        );
      })}
    </ul>
  );
}

function Profile({ p }: { p: Record<string, any> }) {
  const zones: any[] = p.delivery?.zones ?? [];
  const weekly: any[] = p.hours?.weekly ?? [];
  const policies: Record<string, string> = p.policies ?? {};
  return (
    <div className="grid grid-cols-1 gap-5 p-5 lg:grid-cols-2">
      <div className="space-y-5">
        <div>
          <SectionLabel className="flex items-center gap-1.5">
            <Store size={13} /> The shop
          </SectionLabel>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Name">{p.identity?.name}</Field>
            <Field label="Owner">{p.identity?.owner_name}</Field>
            <Field label="Address">{p.contact?.address}</Field>
            <Field label="Category">{p.identity?.category}</Field>
          </div>
          {p.identity?.description && <p className="mt-3 text-[13px] text-dusk">{p.identity.description}</p>}
        </div>
        <div>
          <SectionLabel className="flex items-center gap-1.5">
            <Clock3 size={13} /> Opening hours
          </SectionLabel>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-[13px]">
            {weekly.map((d) => (
              <div key={d.day} className="flex justify-between">
                <span className="text-dusk">{DAY[d.day] ?? d.day}</span>
                <span className="tabular text-ink">
                  {(d.intervals ?? []).map((i: any) => `${i.open}–${i.close}`).join(", ") || "Closed"}
                </span>
              </div>
            ))}
          </div>
        </div>
        <div>
          <SectionLabel className="flex items-center gap-1.5">
            <Wallet size={13} /> Payments
          </SectionLabel>
          <div className="flex flex-wrap gap-1.5">
            {(p.payments?.methods ?? []).map((m: string) => (
              <Badge key={m}>{m.replace(/_link$/, "").replace(/_/g, " ").toUpperCase()}</Badge>
            ))}
          </div>
        </div>
      </div>
      <div className="space-y-5">
        <div>
          <SectionLabel className="flex items-center gap-1.5">
            <MapPin size={13} /> Delivery
          </SectionLabel>
          {zones.map((z) => (
            <div key={z.id} className="mb-2 rounded-lg border border-sand p-3">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-medium text-ink">{z.label}</span>
                <span className="tabular text-[13px] text-ink">
                  {inr((z.fee_paise ?? 0) / 100)}
                  {z.free_above_paise ? <span className="text-stone"> · free above {inr(z.free_above_paise / 100)}</span> : null}
                </span>
              </div>
              <div className="mt-1 text-xs text-stone">{(z.areas ?? []).join(", ")}</div>
            </div>
          ))}
          <div className="text-xs text-stone">
            Order at least {p.delivery?.notice_hours ?? "-"} h ahead{p.delivery?.pickup_available ? " · pickup available" : ""}
          </div>
        </div>
        <div>
          <SectionLabel className="flex items-center gap-1.5">
            <ScrollText size={13} /> Policies
          </SectionLabel>
          <div className="space-y-2.5">
            {Object.entries(policies).map(([k, v]) => (
              <div key={k}>
                <div className="text-[12.5px] font-medium capitalize text-ink">{k.replace(/_/g, " ")}</div>
                <div className="text-[13px] text-dusk">{String(v)}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export function Knowledge() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as View) || "catalogue";
  const q = useQuery({
    queryKey: ["knowledge", bid],
    queryFn: () => api.get<KnowledgeView>("/knowledge", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 10000,
  });
  const k = q.data;
  return (
    <div className="space-y-5">
      <PageHeader title="Knowledge">What the AI answers from right now. Every price, answer and policy comes from here and nowhere else.</PageHeader>
      <Segmented<View>
        value={view}
        onChange={(v) => setParams(v === "catalogue" ? {} : { view: v }, { replace: true })}
        options={[
          { value: "catalogue", label: <span>Catalogue <span className="tabular ml-1 text-stone">{k?.catalogue.length ?? ""}</span></span> },
          { value: "qa", label: <span>Saved answers <span className="tabular ml-1 text-stone">{k?.qa.length ?? ""}</span></span> },
          { value: "profile", label: "Shop profile" },
        ]}
      />
      <ErrorNote error={q.error} />
      <Card flush className="overflow-hidden">
        {!k ? (
          <div className="p-5">
            <Skeleton lines={8} />
          </div>
        ) : view === "catalogue" ? (
          <Catalogue items={k.catalogue} />
        ) : view === "qa" ? (
          <QA items={k.qa} />
        ) : (
          <Profile p={k.profile} />
        )}
      </Card>
    </div>
  );
}
