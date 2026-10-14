import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Minus, Pencil, Plus, type LucideIcon, Undo2 } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Json, PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Button, Card, cx, Empty, ErrorNote, Mono, Segmented, Skeleton } from "../components/ui";
import { api, type ChangeSetRow, type OwnerWorkspace } from "../lib/api";
import { useSession } from "../lib/session";
import { OwnerFiles } from "../components/owner";
import { Imports } from "./Imports";

const OP_ICON: Record<string, LucideIcon> = { create: Plus, update: Pencil, delete: Minus, archive: Minus };

// the fields of an item that changed, before → after (a create shows its new values)
function diff(before: Record<string, unknown> | null, after: Record<string, unknown> | null): [string, unknown, unknown][] {
  const keys = new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})]);
  return [...keys]
    .filter((k) => JSON.stringify(before?.[k]) !== JSON.stringify(after?.[k]))
    .map((k) => [k, before?.[k], after?.[k]]);
}

const show = (v: unknown) => (v === undefined ? "∅" : typeof v === "string" ? v : JSON.stringify(v));

function OpIcon({ op }: { op: string }) {
  const Icon = OP_ICON[op] ?? Pencil;
  const tone = op === "create" ? "bg-verdant-l text-verdant" : op === "delete" || op === "archive" ? "bg-ember-l text-ember" : "bg-sky-l text-sky";
  return (
    <span className={cx("flex h-5 w-5 items-center justify-center rounded", tone)}>
      <Icon size={12} />
    </span>
  );
}

function Item({ it }: { it: ChangeSetRow["items"][number] }) {
  const [raw, setRaw] = useState(false);
  const changes = diff(it.before, it.after);
  return (
    <li className="rounded-lg bg-bone px-2.5 py-1.5">
      <div className="flex items-center gap-2 text-xs">
        <OpIcon op={it.op} />
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
              {it.op !== "create" && <span className="text-ember line-through">{show(b).slice(0, 120)}</span>} <span className="text-[#00785a]">{show(a).slice(0, 160)}</span>
            </li>
          ))}
          {changes.length > 8 && <li className="text-[11px] text-stone">…and {changes.length - 8} more fields</li>}
        </ul>
      )}
    </li>
  );
}

function ChangeLog() {
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
      <ErrorNote error={q.error ?? undo.error} />
      <Card>
        {q.data?.length ? (
          <ul className="space-y-4">
            {q.data.map((cs) => (
              <li key={cs.id} className="border-t border-sand/60 pt-3 first:border-0 first:pt-0">
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

type View = "changes" | "imports" | "files";

export function History() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as View) || "changes";
  const owner = useQuery({
    queryKey: ["owner", bid],
    queryFn: () => api.get<OwnerWorkspace>("/owner", { business_id: bid }),
    enabled: !!bid && view === "files",
    refetchInterval: 10000,
  });
  return (
    <div className="space-y-5">
      <PageHeader title="History">
        Every change to what the AI knows, as one undoable step, and where it came from: the owner's words, an imported menu or price list, or a
        decision it learned from. Undo here is the owner's <Mono>/undo</Mono>, refused if the data changed since.
      </PageHeader>
      <Segmented<View>
        value={view}
        onChange={(v) => setParams(v === "changes" ? {} : { view: v }, { replace: true })}
        options={[
          { value: "changes", label: "Changes" },
          { value: "imports", label: "Imports" },
          { value: "files", label: "Owner's files" },
        ]}
      />
      {view === "changes" && <ChangeLog />}
      {view === "imports" && <Imports />}
      {view === "files" && (
        <Card flush>
          <ErrorNote error={owner.error} />
          {owner.data ? <OwnerFiles items={owner.data.files} /> : <div className="p-5"><Skeleton lines={4} /></div>}
        </Card>
      )}
    </div>
  );
}
