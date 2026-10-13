import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenCheck, Brain, MessageSquareQuote, Undo2 } from "lucide-react";

import { PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Skeleton, cx } from "../components/ui";
import { api, type PlaybookProposal, type PlaybookRuleRow } from "../lib/api";
import { useSession } from "../lib/session";

const EFFECT: Record<string, { tone: "good" | "bad" | "warn"; label: string }> = {
  allow: { tone: "good", label: "Does it" },
  deny: { tone: "bad", label: "Declines" },
  propose: { tone: "warn", label: "Asks first" },
};

function Rule({ r, onUndo, busy }: { r: PlaybookRuleRow; onUndo: () => void; busy: boolean }) {
  const effect = EFFECT[r.effect] ?? { tone: "warn" as const, label: r.effect };
  return (
    <li className={cx("rounded-lg border px-3 py-2.5", r.status === "active" ? "border-sand" : "border-dashed border-sand opacity-60")}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={effect.tone}>{effect.label}</Badge>
        {r.customer ? <Badge tone="info">only {r.customer}</Badge> : <Badge>everyone</Badge>}
        {r.status !== "active" && <StatusBadge status={r.status} />}
        <span className="ml-auto text-[11px] text-stone">{fmtWhen(r.created_at)}</span>
        {r.status === "active" && r.change_set_id && (
          <Button variant="ghost" className="text-xs" onClick={onUndo} disabled={busy} title="The same undo as /undo on the owner's phone">
            <Undo2 size={12} /> Undo
          </Button>
        )}
      </div>
      <div className="mt-1.5 text-sm font-semibold">“{r.readback}”</div>
      <div className="mt-1 text-xs text-dusk">
        {r.source === "owner_decision" ? (
          <>
            <Brain size={12} className="mr-1 inline text-signal" />
            Learned from the owner's decision
            {r.source_decision?.summary && <> on <span className="font-semibold">{r.source_decision.summary}</span></>}
            {r.source_decision?.decided_at && <> · {fmtWhen(r.source_decision.decided_at)}</>}
          </>
        ) : (
          <>
            <MessageSquareQuote size={12} className="mr-1 inline text-signal" />
            The owner said: “{r.says}”
          </>
        )}
      </div>
    </li>
  );
}


export function Playbook() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["playbook", bid],
    queryFn: () => api.get<{ rules: PlaybookRuleRow[]; proposals: PlaybookProposal[] }>("/playbook", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 4000,
  });
  const undo = useMutation({
    mutationFn: (changeSetId: string) => api.post(`/catalogue/change-sets/${changeSetId}/revert`, {}, { business_id: bid }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["playbook", bid] }),
  });
  const rules = q.data?.rules ?? [];
  const active = rules.filter((r) => r.status === "active");
  const retired = rules.filter((r) => r.status !== "active");
  const byCap = new Map<string, PlaybookRuleRow[]>();
  for (const r of active) byCap.set(r.capability, [...(byCap.get(r.capability) ?? []), r]);
  const CAP_TITLE: Record<string, string> = {
    "order.cancel": "Cancelling an order",
    "order.change": "Changing a placed order",
    "refund.request": "Refunds",
  };

  return (
    <div className="space-y-5">
      <PageHeader title="Rules">
        What the AI may do on its own, must decline, or must ask about first — each rule read back word for word, with where it came from.
        Undo works exactly like the owner's /undo.
      </PageHeader>
      <ErrorNote error={q.error ?? undo.error} />
      {!q.data ? (
        <Card>
          <Skeleton lines={6} />
        </Card>
      ) : byCap.size ? (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
          {[...byCap.entries()].map(([cap, rs]) => (
            <Card key={cap} title={CAP_TITLE[cap] ?? cap} actions={<Badge tone="good">{rs.length} active</Badge>}>
              <ul className="space-y-2.5">
                {rs.map((r) => (
                  <Rule key={r.id} r={r} busy={undo.isPending} onUndo={() => r.change_set_id && undo.mutate(r.change_set_id)} />
                ))}
              </ul>
            </Card>
          ))}
        </div>
      ) : (
        <Card>
          <Empty icon={BookOpenCheck} hint="A rule appears when the owner taps “Always” after deciding, or teaches one in their own words.">
            No active rules — the AI asks the owner whenever it is unsure
          </Empty>
        </Card>
      )}
      {retired.length > 0 && (
        <Card title={`Retired rules · ${retired.length}`}>
          <ul className="space-y-2.5">
            {retired.slice(0, 10).map((r) => (
              <Rule key={r.id} r={r} busy onUndo={() => undefined} />
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
