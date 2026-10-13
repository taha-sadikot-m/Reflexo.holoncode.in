import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, MessageSquareQuote, Undo2 } from "lucide-react";
import { Link } from "react-router-dom";

import { PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, cx } from "../components/ui";
import { api, type PlaybookProposal, type PlaybookRuleRow } from "../lib/api";
import { useSession } from "../lib/session";

const EFFECT: Record<string, { tone: "good" | "bad" | "warn"; label: string }> = {
  allow: { tone: "good", label: "does it" },
  deny: { tone: "bad", label: "says no" },
  propose: { tone: "warn", label: "asks first" },
};

const LEARNED: Record<string, string> = { g: "learned: always", c: "learned: this customer", once: "just this once" };

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

function ProposalRow({ p }: { p: PlaybookProposal }) {
  return (
    <li className="border-t border-sand/60 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{p.label}</span>
        <span className="text-dusk">{p.summary}</span>
        <StatusBadge status={p.status} />
        {p.done === false && p.status === "approved" && <Badge tone="bad">could not be done</Badge>}
        {p.learned && <Badge tone={p.learned === "once" ? "neutral" : "accent"}>{LEARNED[p.learned] ?? p.learned}</Badge>}
        {p.owner_initiated && <Badge>owner's own action</Badge>}
      </div>
      <div className="mt-0.5 text-xs text-stone">
        {p.customer ?? "—"}
        {p.reason && ` · “${p.reason}”`} · asked {fmtWhen(p.created_at)}
        {p.decided_at && ` · decided ${fmtWhen(p.decided_at)}${p.decided_via ? ` via ${p.decided_via}` : ""}`}
        {p.conversation_id && (
          <>
            {" · "}
            <Link to={`/conversations?c=${p.conversation_id}`} className="text-signal hover:underline">
              chat
            </Link>
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
  const byCap = new Map<string, PlaybookRuleRow[]>();
  for (const r of rules) byCap.set(r.capability, [...(byCap.get(r.capability) ?? []), r]);
  const pending = (q.data?.proposals ?? []).filter((p) => p.status === "pending").length;

  return (
    <div className="space-y-5">
      <PageHeader title="Playbook">
        How this shop works, learned from the owner. When a customer asks for something the AI is not sure it may do, it asks the owner, carries out
        their decision exactly, and offers to remember it as a rule — read back word for word, and undoable.
      </PageHeader>
      <ErrorNote error={q.error ?? undo.error} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <Card title={`Rules · ${rules.filter((r) => r.status === "active").length} active`}>
          {byCap.size ? (
            <div className="space-y-4">
              {[...byCap.entries()].map(([cap, rs]) => (
                <div key={cap}>
                  <div className="mb-1.5 font-mono text-[11px] text-stone">{cap}</div>
                  <ul className="space-y-2">
                    {rs.map((r) => (
                      <Rule key={r.id} r={r} busy={undo.isPending} onUndo={() => r.change_set_id && undo.mutate(r.change_set_id)} />
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          ) : (
            <Empty>No rules yet. They appear when the owner says “Always” after deciding, or teaches one in words.</Empty>
          )}
        </Card>
        <Card title={`Asked the owner${pending ? ` · ${pending} waiting` : ""}`}>
          {q.data?.proposals.length ? (
            <ul>
              {q.data.proposals.map((p) => (
                <ProposalRow key={p.id} p={p} />
              ))}
            </ul>
          ) : (
            <Empty>Nothing asked yet.</Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
