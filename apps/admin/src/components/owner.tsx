// What the AI sent the owner, and the files the owner sent: shown in Settings (log) and Brain → History (sources).
import { BellRing, FileSpreadsheet, FileText, FolderOpen, Image as ImageIcon, Paperclip } from "lucide-react";
import { Link } from "react-router-dom";

import type { OwnerWorkspace } from "../lib/api";
import { fmtWhen } from "./bits";
import { channelLabel } from "./channel";
import { Badge, Empty, cx } from "./ui";

const KIND: Record<string, string> = {
  question: "Question",
  question_followup: "Customer asked again",
  proposal: "Decision needed",
  action_done: "Done under a rule",
  learn: "Offer to learn",
  handoff: "Customer needs a person",
  human_waiting: "Customer waiting on a person",
  order_confirmed: "New order",
  refund_due: "Refund due",
};

export function OwnerNotifications({ items }: { items: OwnerWorkspace["notifications"] }) {
  if (!items.length) return <Empty icon={BellRing}>Nothing sent to the owner yet</Empty>;
  return (
    <ul className="divide-y divide-sand">
      {items.map((n, i) => (
        <li key={i} className="px-5 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-medium text-ink">{KIND[n.kind] ?? n.kind.replace(/_/g, " ")}</span>
            {n.priority === "p0" && <Badge tone="bad">Urgent</Badge>}
            {n.status !== "sent" && <Badge tone={n.status === "failed" ? "bad" : "neutral"}>{n.status}</Badge>}
            <span className="ml-auto text-xs text-stone">
              {n.delivered_via && `${channelLabel(n.delivered_via)} · `}
              {fmtWhen(n.delivered_at ?? n.created_at)}
            </span>
          </div>
          {n.text && <div className="mt-1 line-clamp-3 whitespace-pre-wrap text-[12.5px] text-dusk">{n.text}</div>}
        </li>
      ))}
    </ul>
  );
}

function FileTypeIcon({ mime }: { mime: string }) {
  const Icon = mime.startsWith("image/") ? ImageIcon : mime.includes("pdf") ? FileText : mime.includes("sheet") || mime.includes("csv") ? FileSpreadsheet : Paperclip;
  return (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-bone text-dusk">
      <Icon size={17} />
    </span>
  );
}

const size = (b: number) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`);

export function OwnerFiles({ items }: { items: OwnerWorkspace["files"] }) {
  if (!items.length) return <Empty icon={FolderOpen} hint="Menus, price lists and photos the owner sends land here.">No files yet</Empty>;
  return (
    <ul className="divide-y divide-sand">
      {items.map((f) => (
        <li key={f.id} className={cx("flex items-start gap-3 px-5 py-3")}>
          <FileTypeIcon mime={f.mime} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13.5px] font-medium text-ink">{f.filename}</div>
            {f.description && <div className="line-clamp-2 text-xs text-dusk">{f.description}</div>}
            <div className="mt-0.5 text-[11.5px] text-stone">
              {f.kind} · {size(f.size_bytes)} · {f.received_via ? channelLabel(f.received_via) : "—"} · {fmtWhen(f.created_at)}
              {f.imports > 0 && (
                <>
                  {" · "}
                  <Link to="/brain/history?view=imports" className="text-signal hover:text-signal-d">
                    {f.imports} import{f.imports > 1 ? "s" : ""}
                  </Link>
                </>
              )}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}
