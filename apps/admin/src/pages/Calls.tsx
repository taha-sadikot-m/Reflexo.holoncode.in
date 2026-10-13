import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Image as ImageIcon, PhoneIncoming, PhoneOutgoing } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader, StatusBadge, fmtWhen } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type CallRow } from "../lib/api";
import { useSession } from "../lib/session";

const dur = (s: number | null) => (s == null ? "—" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);

function Call({ c }: { c: CallRow }) {
  const live = c.status === "active" || c.status === "ringing" || c.status === "connecting";
  const [open, setOpen] = useState(live);
  return (
    <li className="border-t border-sand/60 py-3 first:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => setOpen(!open)} className="text-sm font-semibold hover:underline">
          <ChevronRight size={13} className={cx("inline transition-transform", open && "rotate-90")} />{" "}
          {c.direction === "inbound" ? <PhoneIncoming size={14} className="inline text-signal" /> : <PhoneOutgoing size={14} className="inline text-signal" />}{" "}
          {c.caller ?? "Unknown caller"}
        </button>
        <StatusBadge status={c.status} />
        {live && <Badge tone="bad">● live</Badge>}
        <Badge>{c.path}</Badge>
        <Mono>{dur(c.seconds)}</Mono>
        <span className="ml-auto text-[11px] text-stone">{fmtWhen(c.started_at)}</span>
      </div>
      {c.summary && <div className="mt-1 text-sm text-dusk">{c.summary}</div>}
      {open && (
        <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-[1fr_280px]">
          <div className="max-h-96 space-y-1 overflow-y-auto rounded-lg bg-bone p-2.5">
            {c.transcript.length ? (
              c.transcript.map((t, i) => (
                <div key={i} className={cx("text-sm", t.role === "agent" || t.role === "assistant" ? "text-signal" : "")}>
                  <span className="mr-1 text-[10px] font-bold uppercase text-stone">{t.role ?? "?"}</span>
                  {t.text}
                </div>
              ))
            ) : (
              <div className="text-xs text-stone">No transcript.</div>
            )}
          </div>
          <div className="space-y-1">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-stone">In-call events</div>
            {c.events.length ? (
              c.events.map((e, i) => (
                <div key={i} className="text-xs">
                  <Mono>{e.kind}</Mono> <StatusBadge status={e.status} />{" "}
                  <span className="text-stone">{fmtWhen(e.at)}</span>
                  {typeof e.payload?.text === "string" && <div className="text-dusk">{e.payload.text}</div>}
                  {typeof e.payload?.description === "string" && <div className="flex items-start gap-1 text-dusk"><ImageIcon size={12} className="mt-0.5 shrink-0" /> {e.payload.description}</div>}
                </div>
              ))
            ) : (
              <div className="text-xs text-stone">None.</div>
            )}
            {c.end_reason && <div className="pt-1 text-[11px] text-stone">ended: {c.end_reason}</div>}
            {c.conversation_id && (
              <Link to={`/conversations?c=${c.conversation_id}`} className="block pt-1 text-xs text-signal hover:underline">
                Conversation →
              </Link>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

export function Calls() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const q = useQuery({
    queryKey: ["calls", bid],
    queryFn: () => api.get<CallRow[]>("/calls", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 3000,
  });
  return (
    <div className="space-y-5">
      <PageHeader title="Calls">Voice calls the AI answered: path, duration, transcript, and what was shared during the call (photos, text).</PageHeader>
      <ErrorNote error={q.error} />
      <Card>
        {q.data?.length ? (
          <ul>
            {q.data.map((c) => (
              <Call key={c.id} c={c} />
            ))}
          </ul>
        ) : (
          <Empty>No calls yet.</Empty>
        )}
      </Card>
    </div>
  );
}
