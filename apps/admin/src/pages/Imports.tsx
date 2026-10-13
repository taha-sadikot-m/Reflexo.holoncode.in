import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Json, PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type ImportRow } from "../lib/api";
import { useSession } from "../lib/session";

function name(x: Record<string, unknown>): string {
  return String(x.name ?? x.title ?? x.question ?? Object.values(x)[0] ?? "item");
}

function ImportCard({ r }: { r: ImportRow }) {
  const [open, setOpen] = useState(false);
  const flagged = r.items.filter((i) => (i.issues?.length ?? 0) > 0 || (i.confidence ?? 1) < 0.7).length;
  return (
    <li className="border-t border-sand/60 py-3 first:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => setOpen(!open)} className="text-sm font-semibold hover:underline">
          <ChevronRight size={13} className={cx("inline transition-transform", open && "rotate-90")} /> {r.filename ?? r.source_kind}
        </button>
        <StatusBadge status={r.status} />
        <Badge>{r.items.length} items</Badge>
        {flagged > 0 && <Badge tone="warn">{flagged} need a look</Badge>}
        {r.change_set_id && (
          <Link to="/brain/history" className="text-xs text-signal hover:underline">
            change set →
          </Link>
        )}
        <span className="ml-auto text-[11px] text-stone">{fmtWhen(r.created_at)}</span>
      </div>
      {r.error && <div className="mt-1 text-xs text-ember">{r.error}</div>}
      {open && (
        <div className="mt-2 space-y-2">
          {r.workflow_id && (
            <div className="text-[11px] text-stone">
              workflow <Mono>{r.workflow_id}</Mono>
            </div>
          )}
          <table className="w-full text-xs">
            <thead className="text-left text-[10px] uppercase tracking-wide text-stone">
              <tr>
                <th className="pb-1">Item</th>
                <th className="pb-1">Confidence</th>
                <th className="pb-1">Issues</th>
                <th className="pb-1">Resolution</th>
              </tr>
            </thead>
            <tbody>
              {r.items.map((i, n) => (
                <tr key={n} className="border-t border-sand/40 align-top">
                  <td className="py-1 pr-2" title={JSON.stringify(i.extracted)}>
                    {name(i.extracted)}
                  </td>
                  <td className={cx("py-1 font-mono", (i.confidence ?? 1) < 0.7 && "text-sun")}>{i.confidence != null ? Number(i.confidence).toFixed(2) : "—"}</td>
                  <td className="py-1 text-sun">{i.issues?.join(", ")}</td>
                  <td className="py-1">{i.resolution}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {Object.keys(r.stats).length > 0 && <Json value={r.stats} />}
        </div>
      )}
    </li>
  );
}

export function Imports() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const q = useQuery({
    queryKey: ["imports", bid],
    queryFn: () => api.get<ImportRow[]>("/imports", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  return (
    <div className="space-y-5">
      <PageHeader title="Imports">Menus and price lists the owner sent: what was read, how sure the AI was, and the change it became.</PageHeader>
      <ErrorNote error={q.error} />
      <Card>
        {q.data?.length ? (
          <ul>
            {q.data.map((r) => (
              <ImportCard key={r.id} r={r} />
            ))}
          </ul>
        ) : (
          <Empty>No imports yet.</Empty>
        )}
      </Card>
    </div>
  );
}
