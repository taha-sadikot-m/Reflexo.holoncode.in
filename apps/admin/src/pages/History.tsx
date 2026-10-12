import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Undo2 } from "lucide-react";
import { useState } from "react";

import { Json, PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono } from "../components/ui";
import { api, type ChangeSetRow } from "../lib/api";
import { useSession } from "../lib/session";

const OP_ICON: Record<string, string> = { create: "＋", update: "✎", delete: "－", archive: "－" };

// the fields of an item that changed, before → after (a create shows its new values)
function diff(before: Record<string, unknown> | null, after: Record<string, unknown> | null): [string, unknown, unknown][] {
  const keys = new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})]);
  return [...keys]
    .filter((k) => JSON.stringify(before?.[k]) !== JSON.stringify(after?.[k]))
    .map((k) => [k, before?.[k], after?.[k]]);
}

const show = (v: unknown) => (v === undefined ? "∅" : typeof v === "string" ? v : JSON.stringify(v));

function Item({ it }: { it: ChangeSetRow["items"][number] }) {
  const [raw, setRaw] = useState(false);
  const changes = diff(it.before, it.after);
  return (
    <li className="rounded-lg bg-bone px-2.5 py-1.5 dark:bg-ink">
      <div className="flex items-center gap-2 text-xs">
        <span className="font-bold">{OP_ICON[it.op] ?? it.op}</span>
        <Mono>{it.entity}</Mono>
        <span className="text-stone">{it.op}</span>
        <button className="ml-auto text-[11px] text-signal hover:underline" onClick={() => setRaw(!raw)}>
          {raw ? "diff" : "raw"}
        </button>
      </div>
      {raw ? (
        <Json value={{ before: it.before, after: it.after }} className="mt-1" />
      ) : (
        <ul className="mt-0.5 space-y-0.5">
          {changes.slice(0, 8).map(([k, b, a]) => (
            <li key={k} className="break-words text-[11px]">
              <span className="font-semibold">{k}</span>:{" "}
              {it.op !== "create" && <span className="text-ember line-through">{show(b).slice(0, 120)}</span>} <span className="text-[#00785a] dark:text-verdant">{show(a).slice(0, 160)}</span>
            </li>
          ))}
          {changes.length > 8 && <li className="text-[11px] text-stone">…and {changes.length - 8} more fields</li>}
        </ul>
      )}
    </li>
  );
}

export function History() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["change-sets", bid],
    queryFn: () => api.get<ChangeSetRow[]>("/catalogue/change-sets", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  const undo = useMutation({
    mutationFn: (id: string) => api.post(`/catalogue/change-sets/${id}/revert`, {}, { business_id: bid }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["change-sets", bid] }),
  });
  return (
    <div className="space-y-5">
      <PageHeader title="Knowledge history">
        Every change to what the AI knows — catalogue, prices, Q&amp;A, profile, playbook rules — as an atomic change set. Undo here is the same as
        the owner's <Mono>/undo</Mono>, and is refused if the rows changed since.
      </PageHeader>
      <ErrorNote error={q.error ?? undo.error} />
      <Card>
        {q.data?.length ? (
          <ul className="space-y-4">
            {q.data.map((cs) => (
              <li key={cs.id} className="border-t border-sand/60 pt-3 first:border-0 first:pt-0 dark:border-twilight">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold">{cs.summary}</span>
                  <StatusBadge status={cs.status} />
                  <Badge tone="info">{cs.origin.replace(/_/g, " ")}</Badge>
                  {cs.revert_of && <Badge>undo</Badge>}
                  <span className="ml-auto text-[11px] text-stone">{fmtWhen(cs.applied_at ?? cs.created_at)}</span>
                  {cs.status === "applied" && !cs.revert_of && (
                    <Button
                      variant="ghost"
                      className="text-xs"
                      disabled={undo.isPending}
                      onClick={() => window.confirm(`Undo “${cs.summary}”?`) && undo.mutate(cs.id)}
                    >
                      <Undo2 size={12} /> Undo
                    </Button>
                  )}
                </div>
                <ul className="mt-2 grid grid-cols-1 gap-1.5 lg:grid-cols-2">
                  {cs.items.map((it, i) => (
                    <Item key={i} it={it} />
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>No changes yet.</Empty>
        )}
      </Card>
    </div>
  );
}
