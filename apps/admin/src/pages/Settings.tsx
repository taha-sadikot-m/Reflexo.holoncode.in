import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, ToggleLeft } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Json, PageHeader, StatusBadge } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono, Skeleton } from "../components/ui";
import { api, type OwnerWorkspace } from "../lib/api";
import { OwnerNotifications } from "../components/owner";
import { useSession } from "../lib/session";
import { channelLabel } from "../components/channel";

interface SettingsView {
  flags: Record<string, unknown>;
  connections: {
    id: string;
    channel: string;
    provider: string;
    display_name: string;
    roles: string[];
    customer_mode: string;
    job_description: string | null;
    status: string;
    account: string;
  }[];
}

interface ResetResult {
  businesses: Record<string, string>;
  seconds: number;
  workflows_terminated: number;
  workflows_unreachable: boolean;
}

const MODES = {
  onboarding: "Meera starts onboarding with an empty catalogue, and the demo builds it from a menu photo.",
  full: "Meera is live with the full catalogue, for rehearsing later segments on their own.",
} as const;

export function Settings() {
  const { me, business, setBusinessId } = useSession();
  const bid = business?.id ?? "";
  const qc = useQueryClient();
  const [last, setLast] = useState<ResetResult | null>(null);
  const owner = useQuery({
    queryKey: ["owner", bid],
    queryFn: () => api.get<OwnerWorkspace>("/owner", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 15000,
  });
  const q = useQuery({
    queryKey: ["settings", bid],
    queryFn: () => api.get<SettingsView>("/settings", { business_id: bid }),
    enabled: !!bid,
  });
  const reset = useMutation({
    mutationFn: (mode: keyof typeof MODES) => api.post<ResetResult>("/demo-reset", {}, { mode }),
    onSuccess: async (r) => {
      setLast(r);
      await qc.invalidateQueries({ queryKey: ["businesses"] });
      const meera = r.businesses["meeras-bakes"];
      if (meera) setBusinessId(meera); // ids changed: follow Meera's new id
    },
  });
  const run = (mode: keyof typeof MODES) => {
    if (window.confirm(`Reset ALL demo businesses (${mode})?\n\n${MODES[mode]}\n\nEvery conversation, order, rule and automation is deleted.`)) reset.mutate(mode);
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Settings"
        actions={
          <Link to="/settings/access" className="text-[13px] font-medium text-signal hover:underline">
            Access logs →
          </Link>
        }
      >
        Demo reset, channel connections and runtime flags.
      </PageHeader>
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <Card title="Demo reset">
          <div className="space-y-3">
            {(Object.keys(MODES) as (keyof typeof MODES)[]).map((m) => (
              <div key={m} className="flex items-start gap-3">
                <Button variant={m === "onboarding" ? "danger" : "ghost"} className="shrink-0 text-xs" disabled={reset.isPending} onClick={() => run(m)}>
                  <RotateCcw size={12} /> {m === "onboarding" ? "Reset for the demo" : "Reset with full catalogue"}
                </Button>
                <div className="text-xs text-dusk">
                  {MODES[m]} <Mono>make demo-reset{m === "full" ? " FULL=1" : ""}</Mono>
                </div>
              </div>
            ))}
            {reset.isPending && <div className="text-sm text-signal">Resetting… stopping workflows, wiping, reseeding.</div>}
            <ErrorNote error={reset.error} />
            {last && (
              <div className="rounded-lg bg-verdant-l px-3 py-2 text-sm">
                Done in {last.seconds}s · {last.workflows_terminated} workflow{last.workflows_terminated === 1 ? "" : "s"} stopped ·{" "}
                {Object.keys(last.businesses).join(", ")}
                {last.workflows_unreachable && <div className="text-ember">Some workflows could not be stopped. Is the Temporal worker running?</div>}
                <div className="text-xs text-dusk">Workers pick up the new connections within 30 s.</div>
              </div>
            )}
            <div className="text-xs text-stone">
              Demo time: 1 day of waiting in automations takes {Math.round(86400 * (me?.demo_time_scale ?? 1))} s
            </div>
          </div>
        </Card>
        <Card title="Channel connections">
          <ErrorNote error={q.error} />
          {q.data?.connections.length ? (
            <ul className="space-y-2">
              {q.data.connections.map((c) => (
                <li key={c.id} className="border-t border-sand/60 pt-2 first:border-0 first:pt-0">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-semibold">{c.display_name}</span>
                    <StatusBadge status={c.status} />
                    <Badge tone={c.customer_mode === "off" ? "bad" : "good"} dot>{c.customer_mode === "off" ? "AI paused" : "AI answering"}</Badge>
                    {c.roles.map((r) => (
                      <Badge key={r}>{r}</Badge>
                    ))}
                  </div>
                  <div className="text-[11px] text-stone">
                    {channelLabel(c.channel)}
                    {c.account && (
                      <>
                        {" · "}
                        <Mono>{c.account}</Mono>
                      </>
                    )}
                  </div>
                  {c.job_description && <div className="mt-0.5 text-xs text-dusk">Job: {c.job_description}</div>}
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No connections.</Empty>
          )}
        </Card>
        <Card title="Runtime flags">
          {!q.data ? (
            <Skeleton lines={2} />
          ) : Object.keys(q.data.flags).length ? (
            <Json value={q.data.flags} />
          ) : (
            <Empty icon={ToggleLeft} hint="Kill switches and demo toggles appear here when set.">
              No runtime flags set
            </Empty>
          )}
        </Card>
        <Card title="Sent to the owner" flush className="xl:col-span-2">
          <div className="max-h-[420px] overflow-y-auto">
            {owner.data ? <OwnerNotifications items={owner.data.notifications} /> : <div className="p-5"><Skeleton lines={4} /></div>}
          </div>
        </Card>
      </div>
    </div>
  );
}
