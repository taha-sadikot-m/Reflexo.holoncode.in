import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type OwnerWorkspace } from "../lib/api";
import { useSession } from "../lib/session";

function Sla({ due, done }: { due: string | null; done: boolean }) {
  if (!due || done) return null;
  const late = new Date(due).getTime() < Date.now();
  return <Badge tone={late ? "bad" : "warn"}>{late ? `overdue ${fmtWhen(due)}` : `answer due ${fmtWhen(due)}`}</Badge>;
}

const size = (b: number) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`);

export function Owner() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const q = useQuery({
    queryKey: ["owner", bid],
    queryFn: () => api.get<OwnerWorkspace>("/owner", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 4000,
  });
  const d = q.data;
  return (
    <div className="space-y-5">
      <PageHeader title="Owner">What the AI asked the owner, what it needed approval for, what it told them, and the files they sent.</PageHeader>
      <ErrorNote error={q.error} />
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <Card title="Questions">
          {d?.questions.length ? (
            <ul>
              {d.questions.map((x) => (
                <li key={x.id} className="border-t border-sand/60 py-2 first:border-0 dark:border-twilight">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={x.status} />
                    {x.code && <Mono>#{x.code}</Mono>}
                    <Sla due={x.sla_due_at} done={!!x.answered_at || x.status === "expired" || x.status === "cancelled"} />
                    <span className="ml-auto text-[11px] text-stone">{fmtWhen(x.asked_at)}</span>
                  </div>
                  <div className="mt-1 text-sm">{x.question}</div>
                  {x.ai_guess && <div className="text-xs text-stone">AI guessed: {x.ai_guess}</div>}
                  {x.answer && (
                    <div className="mt-1 rounded-lg bg-verdant-l px-2.5 py-1.5 text-sm dark:bg-[#0a3b2e]">
                      {x.answer}
                      <span className="ml-2 text-[11px] text-dusk dark:text-stone">
                        {x.scope === "customer" ? "for this customer" : x.scope === "global" ? "saved for everyone" : x.scope}
                        {x.relayed_at && " · told the customer"}
                      </span>
                    </div>
                  )}
                  <div className="mt-0.5 text-[11px] text-stone">
                    {x.customer}
                    {x.conversation_id && (
                      <>
                        {" · "}
                        <Link to={`/conversations?c=${x.conversation_id}`} className="text-signal hover:underline">
                          chat
                        </Link>
                      </>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No questions yet.</Empty>
          )}
        </Card>
        <Card title="Approvals">
          {d?.approvals.length ? (
            <ul>
              {d.approvals.map((a) => (
                <li key={a.id} className="flex flex-wrap items-center gap-2 border-t border-sand/60 py-2 text-sm first:border-0 dark:border-twilight">
                  <Mono>{a.kind.replace(/_/g, " ")}</Mono>
                  <StatusBadge status={a.status} />
                  {a.code && <Mono>#{a.code}</Mono>}
                  <span className="min-w-0 flex-1 truncate text-dusk dark:text-stone">{a.summary}</span>
                  <span className="text-[11px] text-stone">
                    {a.decided_at ? `decided ${fmtWhen(a.decided_at)}${a.decided_via ? ` via ${a.decided_via}` : ""}` : fmtWhen(a.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No approvals yet.</Empty>
          )}
        </Card>
        <Card title="Notifications">
          {d?.notifications.length ? (
            <ul>
              {d.notifications.map((n, i) => (
                <li key={i} className="border-t border-sand/60 py-2 first:border-0 dark:border-twilight">
                  <div className="flex flex-wrap items-center gap-2">
                    <Mono>{n.kind}</Mono>
                    <Badge tone={n.priority === "p0" ? "bad" : n.priority === "p1" ? "warn" : "neutral"}>{n.priority.toUpperCase()}</Badge>
                    <StatusBadge status={n.status} />
                    <span className="ml-auto text-[11px] text-stone">
                      {n.delivered_via && `${n.delivered_via} · `}
                      {fmtWhen(n.delivered_at ?? n.created_at)}
                    </span>
                  </div>
                  {n.text && <div className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs text-dusk dark:text-stone">{n.text}</div>}
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No notifications yet.</Empty>
          )}
        </Card>
        <Card title="Files">
          {d?.files.length ? (
            <ul>
              {d.files.map((f) => (
                <li key={f.id} className={cx("flex items-start gap-3 border-t border-sand/60 py-2 first:border-0 dark:border-twilight")}>
                  <span className="text-xl">{f.mime.startsWith("image/") ? "🖼" : f.mime.includes("pdf") ? "📄" : f.mime.includes("sheet") || f.mime.includes("csv") ? "📊" : "📎"}</span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-semibold">{f.filename}</div>
                    {f.description && <div className="line-clamp-2 text-xs text-dusk dark:text-stone">{f.description}</div>}
                    <div className="text-[11px] text-stone">
                      {f.kind} · {size(f.size_bytes)} · {f.received_via ?? "—"} · {fmtWhen(f.created_at)}
                      {f.imports > 0 && (
                        <>
                          {" · "}
                          <Link to="/imports" className="text-signal hover:underline">
                            {f.imports} import{f.imports > 1 ? "s" : ""}
                          </Link>
                        </>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No files yet.</Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
