// Costs (BETA_12 §8.1): what the AI costs, from every metered model call, and each business's daily cap.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Coins, Database, MessageSquare, Percent } from "lucide-react";
import { useState } from "react";

import { PageHeader } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono, Segmented, Skeleton, Stat, td, th, tr } from "../components/ui";
import { api, type CostCap, type CostGroup, type CostOverview } from "../lib/api";
import { useSession } from "../lib/session";

/** Micro-rupees as rupees: two decimals, or four for amounts under a paisa. */
export const micro = (v: number | null | undefined) => {
  if (v == null) return "-";
  const r = v / 1_000_000;
  return `₹${r.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: Math.abs(r) < 0.01 && r !== 0 ? 4 : 2 })}`;
};

const GROUPS: { value: CostGroup; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "business", label: "Business" },
  { value: "feature", label: "Feature" },
  { value: "model", label: "Model" },
  { value: "route", label: "Route" },
];
const RANGES = [
  { value: "1", label: "Today" },
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
];

function istDate(offsetDays = 0) {
  const d = new Date(Date.now() + 5.5 * 3600_000 - offsetDays * 86400_000);
  return d.toISOString().slice(0, 10);
}

function CapRow({ cap, readOnly }: { cap: CostCap; readOnly: boolean }) {
  const qc = useQueryClient();
  const [value, setValue] = useState<string>("");
  const save = useMutation({
    mutationFn: (inr: number) => api.patch<CostCap>(`/cost-caps/${cap.business_id}`, { daily_cap_inr: inr }),
    onSuccess: () => {
      setValue("");
      void qc.invalidateQueries({ queryKey: ["costs"] });
    },
  });
  const pct = cap.daily_cap_micro_inr ? Math.round((cap.today_micro_inr * 100) / cap.daily_cap_micro_inr) : 0;
  const tone = pct >= 100 ? "bad" : pct >= cap.alert_at_pct ? "warn" : "good";
  return (
    <tr className={tr}>
      <td className={td}>{cap.name}</td>
      <td className={td}>
        <Mono>{micro(cap.today_micro_inr)}</Mono>
      </td>
      <td className={td}>
        <Mono>{micro(cap.daily_cap_micro_inr)}</Mono> {cap.default_cap && <span className="text-xs text-stone">default</span>}
      </td>
      <td className={td}>
        <Badge tone={tone}>{pct}%</Badge>
        {cap.alerted_on && <span className="ml-2 text-xs text-stone">warned {cap.alerted_on}</span>}
      </td>
      <td className={td}>
        {!readOnly && (
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              const n = Number(value);
              if (n > 0) save.mutate(n);
            }}
          >
            <input
              type="number"
              min={1}
              max={10000}
              step="any"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="₹ a day"
              aria-label={`New daily cap for ${cap.name}`}
              className="h-8 w-24 rounded-md px-2 text-[13px] ring-1 ring-inset ring-sand focus:outline-none"
            />
            <Button type="submit" variant="ghost" disabled={!value || save.isPending}>
              Set cap
            </Button>
          </form>
        )}
        <ErrorNote error={save.error} />
      </td>
    </tr>
  );
}

export function Costs() {
  const { business, me } = useSession();
  const [group, setGroup] = useState<CostGroup>("day");
  const [days, setDays] = useState("7");
  const [all, setAll] = useState(false);
  const params: Record<string, string> = { group_by: group, from: istDate(Number(days) - 1), to: istDate() };
  if (!all && business) params.business_id = business.id;
  const costs = useQuery({
    queryKey: ["costs", params],
    queryFn: () => api.get<CostOverview>("/costs", params),
    refetchInterval: 30000,
  });
  const d = costs.data;
  const max = Math.max(1, ...(d?.rows ?? []).map((r) => r.cost_micro_inr));

  return (
    <div className="space-y-5">
      <PageHeader
        title="Costs"
        actions={
          <>
            <Segmented value={all ? "all" : "one"} onChange={(v) => setAll(v === "all")} options={[
              { value: "one", label: business?.name ?? "This shop" },
              { value: "all", label: "All shops" },
            ]} />
            <Segmented value={days} onChange={setDays} options={RANGES} />
          </>
        }
      >
        What the AI costs, from every metered model call: per turn and conversation, by feature, model and route. Days are India days.
      </PageHeader>
      <ErrorNote error={costs.error} />
      {!d ? (
        <Skeleton lines={6} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
            <Stat label="Spent" value={micro(d.totals.cost_micro_inr)} icon={Coins} hint={`${d.totals.calls.toLocaleString("en-IN")} model calls`} />
            <Stat
              label="Per customer turn"
              value={micro(d.totals.cost_per_turn_micro_inr)}
              icon={MessageSquare}
              tone={(d.totals.cost_per_turn_micro_inr ?? 0) > 150_000 ? "warn" : "good"}
              hint={`${d.totals.chat_turns} turns · target ₹0.15`}
            />
            <Stat label="Per conversation" value={micro(d.totals.cost_per_conversation_micro_inr)} icon={Database} hint={`${d.totals.conversations} conversations`} />
            <Stat
              label="Cached input"
              value={d.totals.cached_share == null ? "-" : `${Math.round(d.totals.cached_share * 100)}%`}
              icon={Percent}
              hint="share of input tokens read from cache"
            />
          </div>

          <Card title="Breakdown" actions={<Segmented value={group} onChange={setGroup} options={GROUPS} />} flush>
            {d.rows.length === 0 ? (
              <Empty>No metered calls in this period.</Empty>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className={th}>{GROUPS.find((g) => g.value === group)?.label}</th>
                    <th className={th}>Cost</th>
                    <th className={th}>Calls</th>
                    <th className={th}>Cached input</th>
                  </tr>
                </thead>
                <tbody>
                  {d.rows.map((r) => (
                    <tr key={r.key} className={tr}>
                      <td className={td}>
                        <div className="font-medium text-ink">{r.label}</div>
                        <div className="mt-1 h-1.5 rounded-full bg-signal/80" style={{ width: `${Math.max(2, (r.cost_micro_inr * 100) / max)}%` }} />
                      </td>
                      <td className={td}>
                        <Mono>{micro(r.cost_micro_inr)}</Mono>
                      </td>
                      <td className={td}>
                        <Mono>{r.calls.toLocaleString("en-IN")}</Mono>
                      </td>
                      <td className={td}>
                        <Mono>{r.input_tokens ? `${Math.round((r.cached_input_tokens * 100) / r.input_tokens)}%` : "-"}</Mono>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card
            title="Daily caps"
            actions={
              <span className="text-xs text-stone">
                At the alert level the owner is warned once a day. At 100% every route uses the cheapest model.
              </span>
            }
            flush
          >
            {d.caps.length === 0 ? (
              <Empty>No shop has a cap or spent anything today.</Empty>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    <th className={th}>Shop</th>
                    <th className={th}>Today</th>
                    <th className={th}>Cap</th>
                    <th className={th}>Used</th>
                    <th className={th} />
                  </tr>
                </thead>
                <tbody>
                  {d.caps.map((c) => (
                    <CapRow key={c.business_id} cap={c} readOnly={!!me?.read_only} />
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <p className="text-xs text-stone">
            Prices: version <Mono>{d.price_version}</Mono>
            {d.fx && (
              <>
                {" "}· rate ₹{d.fx.usd_inr} to the dollar on {d.fx.day} ({d.fx.source})
              </>
            )}
          </p>
        </>
      )}
    </div>
  );
}
