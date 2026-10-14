// A WhatsApp-style phone that renders the real provider payloads our pipeline sent: the Lab's devices and the
// public demo's customer and owner phones are this one component.
import { AlertTriangle, Check, CheckCheck, CornerDownLeft, CreditCard, FileText, Loader2, Paperclip, Send, Store } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import type { SimThread, SimThreadItem } from "../lib/api";
import { fmtTime } from "../lib/stages";
import { ErrorNote, cx } from "./ui";

export type Option = { id: string; title: string };

/** WhatsApp's own formatting: *bold* and _italic_. */
export function WaText({ text }: { text: string }) {
  const parts = text.split(/(\*[^*\n]+\*|_[^_\n]+_)/g);
  return (
    <>
      {parts.map((part, i) =>
        /^\*[^*\n]+\*$/.test(part) ? (
          <strong key={i} className="font-semibold text-ink">
            {part.slice(1, -1)}
          </strong>
        ) : /^_[^_\n]+_$/.test(part) ? (
          <em key={i}>{part.slice(1, -1)}</em>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function payloadText(p: Record<string, any>): string {
  if (p.type === "text") return p.text?.body ?? "";
  if (p.type === "interactive") {
    const i = p.interactive ?? {};
    return [i.header?.text, i.body?.text, i.footer?.text].filter(Boolean).join("\n");
  }
  if (p.type === "image") return p.image?.caption ?? "";
  if (p.type === "document") return `📄 ${p.document?.filename ?? "file"}`;
  if (p.type === "template") return `[Template] ${p.template?.name}`;
  if (typeof p.text === "string") return p.text; // telegram-shaped
  return JSON.stringify(p).slice(0, 200);
}

export function payloadOptions(p: Record<string, any>): Option[] {
  const action = p.interactive?.action;
  if (!action) return [];
  if (action.buttons) return action.buttons.map((b: any) => ({ id: b.reply.id, title: b.reply.title }));
  return (action.sections ?? []).flatMap((s: any) => (s.rows ?? []).map((r: any) => ({ id: r.id, title: r.title })));
}

/** Our own media (/media/<id>?exp&sig) is served by this origin: load it here, whatever public host signed it. */
export function ownMedia(url: string | undefined): string | undefined {
  if (!url) return url;
  try {
    const u = new URL(url, window.location.origin);
    return u.pathname.startsWith("/media/") ? u.pathname + u.search : url;
  } catch {
    return url;
  }
}

// the gateway's text for a message that is only a file ("[image]"): the file itself is shown instead
const FILE_PLACEHOLDER = /^\[(image|photo|document|file|video|audio|sticker)\]$/i;

function linkOf(p: Record<string, any>): string | null {
  const url = p.interactive?.action?.parameters?.url;
  return typeof url === "string" ? url : null;
}

export function Bubble({ item, onTap, disabled, tapped }: { item: SimThreadItem; onTap: (o: Option) => void; disabled: boolean; tapped?: boolean }) {
  if (item.from !== "business") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-[#d9fdd3] px-3 py-1.5 text-[13px] text-[#111b21] shadow-sm">
          {(item.media ?? []).map((m) =>
            m.kind === "image" ? (
              <img key={m.url} src={ownMedia(m.url)} alt="Photo" className="-mx-1.5 mb-1 mt-0.5 max-h-52 w-[calc(100%+12px)] rounded-xl object-cover" />
            ) : (
              <div key={m.url} className="mb-1 flex items-center gap-2 rounded-lg bg-black/5 px-2 py-1.5">
                <FileText size={16} className="text-[#54656f]" /> {m.filename ?? "file"}
              </div>
            ),
          )}
          {item.option ? (
            <span className="inline-flex items-center gap-1 italic">
              <CornerDownLeft size={12} /> {item.option}
            </span>
          ) : item.media?.length && FILE_PLACEHOLDER.test((item.text ?? "").trim()) ? null : (
            <span className="whitespace-pre-wrap break-words">{item.text}</span>
          )}
          <div className="mt-0.5 flex items-center justify-end gap-1 text-[10px] text-[#667781]">
            {fmtTime(item.at)} <CheckCheck size={12} className="text-[#53bdeb]" />
          </div>
        </div>
      </div>
    );
  }
  const p = item.payload ?? {};
  const options = payloadOptions(p);
  const image = p.type === "image" ? ownMedia(p.image?.link as string | undefined) : undefined;
  const text = payloadText(p);
  const link = linkOf(p);
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-sm bg-white px-3 py-1.5 text-[13px] text-[#111b21] shadow-sm">
        {image && <img src={image} alt="" className="-mx-1.5 mb-1 mt-0.5 max-h-52 w-[calc(100%+12px)] rounded-xl object-cover" />}
        {text && (
          <div className="whitespace-pre-wrap break-words">
            <WaText text={text} />
          </div>
        )}
        {link && (
          <a href={link} target="_blank" rel="noreferrer" className="mt-1.5 block border-t border-black/5 pt-1.5 text-center text-[13px] font-semibold text-[#027eb5]">
            {p.interactive?.action?.parameters?.display_text ?? "Open link"}
          </a>
        )}
        {options.length > 0 && (
          <div className="mt-1.5 flex flex-col gap-0.5 border-t border-black/5 pt-1">
            {options.map((o) => (
              <button
                key={o.id}
                disabled={disabled || tapped}
                onClick={() => onTap(o)}
                className="rounded-lg py-1.5 text-center text-[13px] font-semibold text-[#027eb5] transition hover:bg-[#f0f2f5] disabled:cursor-default disabled:opacity-50"
              >
                {o.title}
              </button>
            ))}
          </div>
        )}
        <div className="mt-0.5 flex items-center justify-end gap-1 text-[10px] text-[#667781]">
          {fmtTime(item.at)}
          {item.status === "failed" ? <AlertTriangle size={11} className="text-ember" /> : item.status === "sent" ? <Check size={11} /> : null}
        </div>
      </div>
    </div>
  );
}

function Typing() {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-1 rounded-2xl rounded-bl-sm bg-white px-3.5 py-3 shadow-sm">
        {[0, 1, 2].map((i) => (
          <span key={i} className="h-1.5 w-1.5 animate-bounce rounded-full bg-[#8696a0]" style={{ animationDelay: `${i * 150}ms` }} />
        ))}
      </div>
    </div>
  );
}

/** Is the business still answering? The customer spoke last, recently (a reply normally lands within seconds). */
export function awaitingReply(thread: SimThread | undefined, now = Date.now()): boolean {
  const last = thread?.items.at(-1);
  return !!last && last.from !== "business" && now - new Date(last.at).getTime() < 45_000;
}

export function Device({
  title,
  subtitle,
  thread,
  error,
  onSend,
  onTap,
  onPay,
  busy,
  actions,
  overlay,
  avatar,
  emptyHint = "No messages yet — say hi",
  placeholder = "Message",
  draft: controlledDraft,
  onDraft,
  onAttach,
  expectReply = true,
  className,
  height = "h-[640px]",
}: {
  title: string;
  subtitle?: ReactNode;
  thread: SimThread | undefined;
  error: unknown;
  onSend: (text: string) => void;
  onTap: (o: Option) => void;
  onPay?: (id: string) => void;
  busy: boolean;
  actions?: ReactNode;
  overlay?: ReactNode;
  avatar?: ReactNode;
  emptyHint?: ReactNode;
  placeholder?: string;
  draft?: string;
  onDraft?: (v: string) => void;
  /** a photo/PDF picked on this phone, with the typed text as its caption */
  onAttach?: (file: File, caption: string) => Promise<void>;
  /** false while the customer is on a call: what they send goes to the call, not to a chat reply */
  expectReply?: boolean;
  className?: string;
  height?: string;
}) {
  const [ownDraft, setOwnDraft] = useState("");
  const draft = controlledDraft ?? ownDraft;
  const setDraft = onDraft ?? setOwnDraft;
  const scroller = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [attaching, setAttaching] = useState(false);
  const count = thread?.items.length ?? 0;
  const typing = expectReply && awaitingReply(thread);
  useEffect(() => {
    // scroll the chat only (scrollIntoView also scrolls the page), and return nothing (a returned value is a cleanup)
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [count, typing]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!draft.trim()) return;
    onSend(draft.trim());
    setDraft("");
  };
  const unpaid = (thread?.payment_links ?? []).filter((l) => l.status === "created");
  // buttons are one-shot, like on a phone: once the person answered after a message, its buttons are spent
  const items = thread?.items ?? [];
  return (
    <div className={cx("min-w-0 rounded-[34px] bg-[#0f0b2b] p-2 shadow-pop ring-1 ring-black/10", className)}>
      <div className={cx("relative flex flex-col overflow-hidden rounded-[26px] bg-[#efeae2]", height)}>
        <div className="flex items-center gap-2.5 bg-[#008069] px-4 py-2.5 text-white">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/20">{avatar ?? <Store size={17} />}</div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-semibold">{title}</div>
            <div className="truncate text-[11px] opacity-85">{typing ? "typing…" : subtitle}</div>
          </div>
          {actions}
        </div>
        <div ref={scroller} className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 py-3">
          <ErrorNote error={error} />
          {items.length ? (
            items.map((it, i) => (
              <Bubble key={i} item={it} onTap={onTap} disabled={busy} tapped={it.from === "business" && items.slice(i + 1).some((n) => n.from !== "business")} />
            ))
          ) : (
            <div className="px-4 py-16 text-center text-[12.5px] text-[#667781]">{emptyHint}</div>
          )}
          {typing && <Typing />}
        </div>
        {onPay && unpaid.length > 0 && (
          <div className="flex flex-wrap gap-1.5 border-t border-black/5 bg-white/80 px-3 py-2">
            {unpaid.map((l) => (
              <button
                key={l.id}
                onClick={() => onPay(l.id)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-full bg-[#008069] px-3 py-1.5 text-[12px] font-semibold text-white shadow-sm transition hover:bg-[#017561] disabled:opacity-50"
              >
                <CreditCard size={13} /> Pay {l.order_number} · ₹{(l.amount_paise / 100).toLocaleString("en-IN")} (test)
              </button>
            ))}
          </div>
        )}
        <form onSubmit={submit} className="flex items-center gap-2 bg-[#f0f2f5] px-2 py-2">
          {onAttach && (
            <>
              <input
                ref={picker}
                type="file"
                accept="image/jpeg,image/png,image/webp,application/pdf"
                className="hidden"
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (!file) return;
                  setAttaching(true);
                  try {
                    await onAttach(file, draft.trim());
                    setDraft("");
                  } finally {
                    setAttaching(false);
                  }
                }}
              />
              <button
                type="button"
                onClick={() => picker.current?.click()}
                disabled={busy || attaching}
                className="rounded-full p-2 text-[#54656f] transition hover:bg-black/5 disabled:opacity-40"
                title="Send a photo or PDF"
              >
                {attaching ? <Loader2 size={18} className="animate-spin" /> : <Paperclip size={18} />}
              </button>
            </>
          )}
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={placeholder}
            className="min-w-0 flex-1 rounded-full bg-white px-4 py-2 text-[13.5px] text-[#111b21] outline-none placeholder:text-[#8696a0]"
          />
          <button type="submit" disabled={busy || !draft.trim()} className="rounded-full bg-[#008069] p-2.5 text-white transition disabled:opacity-40">
            <Send size={16} />
          </button>
        </form>
        {overlay}
      </div>
    </div>
  );
}
