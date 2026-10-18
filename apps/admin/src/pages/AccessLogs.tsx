// Access logs (BETA_13 §3, BETA_10 §4 and §13): who looked at a shop's customer data, and every read of a stored
// credential. Read only; both logs are append-only.
import { useInfiniteQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "../components/bits";
import { Badge, Button, Card, Empty, ErrorNote, Mono, Segmented, Skeleton, td, th, tr } from "../components/ui";
import { api, type AccessEntry } from "../lib/api";
import { useSession } from "../lib/session";

type Kind = "operator" | "credential";

export function AccessLogs() {
  const { business } = useSession();
  const [kind, setKind] = useState<Kind>("operator");
  const [all, setAll] = useState(true);
  const params: Record<string, string> = { kind };
  if (!all && business) params.business_id = business.id;
  const log = useInfiniteQuery({
    queryKey: ["access-log", params],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      api.get<{ items: AccessEntry[]; next_before: string | null }>("/access-log", pageParam ? { ...params, before: pageParam } : params),
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const rows = log.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-5">
      <Link to="/settings" className="inline-flex items-center gap-1 text-xs font-medium text-signal hover:underline">
        <ArrowLeft size={12} /> Settings
      </Link>
      <PageHeader
        title="Access logs"
        actions={
          <>
            <Segmented value={all ? "all" : "one"} onChange={(v) => setAll(v === "all")} options={[
              { value: "all", label: "All shops" },
              { value: "one", label: business?.name ?? "This shop" },
            ]} />
            <Segmented value={kind} onChange={setKind} options={[
              { value: "operator", label: "Customer data views" },
              { value: "credential", label: "Credential reads" },
            ]} />
          </>
        }
      >
        {kind === "operator"
          ? "Every time an operator or reviewer opened a shop's conversations, customers, orders or traces. The owner can see these on request."
          : "Every read of a stored token or key from the vault: which part of the system read it, and why."}
      </PageHeader>
      <ErrorNote error={log.error} />
      <Card flush>
        {!log.data ? (
          <Skeleton lines={6} />
        ) : rows.length === 0 ? (
          <Empty>Nothing logged yet.</Empty>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className={th}>When</th>
                <th className={th}>Shop</th>
                <th className={th}>{kind === "operator" ? "Who" : "Read by"}</th>
                <th className={th}>{kind === "operator" ? "What" : "Credential"}</th>
                <th className={th}>{kind === "operator" ? "Request" : "Purpose"}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.at}-${i}`} className={tr}>
                  <td className={td}>
                    <span className="text-xs">{new Date(r.at).toLocaleString()}</span>
                  </td>
                  <td className={td}>{r.business}</td>
                  <td className={td}>
                    <Mono>{r.who}</Mono>
                  </td>
                  <td className={td}>
                    {r.what.replaceAll("_", " ")} {!r.ok && <Badge tone="bad">failed</Badge>}
                  </td>
                  <td className={td}>
                    <Mono className="text-stone">{r.detail}</Mono>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {log.hasNextPage && (
        <Button variant="ghost" onClick={() => void log.fetchNextPage()} disabled={log.isFetchingNextPage}>
          Older entries
        </Button>
      )}
    </div>
  );
}
