import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Ban,
  Check,
  CheckCheck,
  Clock3,
  CreditCard,
  GalleryHorizontal,
  Hand,
  PanelRightClose,
  PanelRightOpen,
  PhoneCall,
  UserRound,
  ListChecks,
  MessagesSquare,
  Package,
  Paperclip,
  Receipt,
  Search,
  Send,
  Sparkles,
  UserCheck,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { StatusBadge, fmtWhen, inr } from "../components/bits";
import { ChannelIcon, LINKED, channelLabel } from "../components/channel";
import { Badge, Button, Card, Empty, ErrorNote, Segmented, Skeleton, cx } from "../components/ui";
import { WaText, plain } from "../components/phone";
import { api, type CallRow, type ConversationItem, type CustomerView, type Thread, type ThreadMessage } from "../lib/api";
import { useSession } from "../lib/session";
import { fmtTime } from "../lib/stages";
import { useXrayStream } from "../lib/stream";

const TICKS: Record<string, { icon: LucideIcon; tone: string; title: string }> = {
  queued: { icon: Clock3, tone: "text-stone", title: "Queued" },
  sending: { icon: Clock3, tone: "text-stone", title: "Sending" },
  unconfirmed: { icon: AlertTriangle, tone: "text-sun", title: "Outcome unknown, retrying safely" },
  sent: { icon: Check, tone: "text-stone", title: "Sent" },
  delivered: { icon: CheckCheck, tone: "text-stone", title: "Delivered" },
  read: { icon: CheckCheck, tone: "text-sky", title: "Read" },
  failed: { icon: AlertTriangle, tone: "text-ember", title: "Failed" },
  blocked: { icon: Ban, tone: "text-ember", title: "Blocked by policy" },
};

const BLOCK_CHIP: Record<string, { icon: LucideIcon; label: string }> = {
  product: { icon: Package, label: "Product card" },
  carousel: { icon: GalleryHorizontal, label: "Carousel" },
  payment_link: { icon: CreditCard, label: "Payment link" },
  order_summary: { icon: Receipt, label: "Order summary" },
};

function initials(name: string | null | undefined): string {
  const parts = (name ?? "?").trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

/** WhatsApp formatting as the phone shows it: *bold* and _italic_ (the raw asterisks were visible here). */
function BlockChips({ blocks }: { blocks: Record<string, unknown>[] | null }) {
  if (!blocks) return null;
  const chips: { icon: LucideIcon; label: string }[] = [];
  for (const b of blocks) {
    const known = BLOCK_CHIP[String(b.type)];
    if (known) chips.push(known);
    else if (b.type === "buttons" || b.type === "list")
      for (const o of (b.options as { title: string }[] | undefined) ?? []) chips.push({ icon: ListChecks, label: o.title });
  }
  if (!chips.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {chips.map((c, i) => (
        <span key={i} className="inline-flex items-center gap-1 rounded-md bg-white/70 px-1.5 py-0.5 text-[11.5px] text-dusk ring-1 ring-inset ring-sand">
          <c.icon size={12} /> {c.label}
        </span>
      ))}
    </div>
  );
}

function Bubble({ m }: { m: ThreadMessage }) {
  const inbound = m.direction === "inbound";
  const tick = TICKS[m.status];
  const who = inbound ? null : m.author === "ai" ? "AI" : m.author === "team" ? "Team" : m.author === "system" ? "System" : m.author;
  const tone = inbound
    ? "rounded-bl-md bg-white ring-1 ring-inset ring-sand"
    : m.author === "ai"
      ? "rounded-br-md bg-signal-l"
      : m.author === "team" || m.author === "owner"
        ? "rounded-br-md bg-sky-l"
        : "rounded-br-md bg-bone";
  return (
    <div className={cx("flex", inbound ? "justify-start" : "justify-end")}>
      <div className={cx("max-w-[72%] rounded-2xl px-3.5 py-2.5 text-[13.5px] leading-relaxed text-soot", tone, m.status === "blocked" && "opacity-60")}>
        {who && (
          <div className="mb-1 flex items-center gap-1 text-[11px] font-semibold text-dusk">
            {m.author === "ai" && <Sparkles size={11} className="text-signal" />}
            {who}
          </div>
        )}
        <div className="whitespace-pre-wrap break-words">
          <WaText text={m.text ?? ""} />
          {m.interactive?.title && <span className="ml-1 text-xs text-dusk">· tapped “{m.interactive.title}”</span>}
        </div>
        {m.attachments.length > 0 && (
          <div className="mt-1.5 inline-flex items-center gap-1 text-xs text-dusk">
            <Paperclip size={12} /> {m.attachments.map((a) => String(a.kind ?? "file")).join(", ")}
          </div>
        )}
        <BlockChips blocks={m.blocks} />
        <div className="mt-1 flex items-center justify-end gap-1.5 text-[11px] text-stone">
          {m.error && <span className="text-ember">{String((m.error as { code?: string }).code ?? "error")}</span>}
          <span>{fmtTime(m.at)}</span>
          {!inbound && tick && (
            <span title={tick.title} className={tick.tone}>
              <tick.icon size={13} />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

const dur = (s: number | null) => (s == null ? "" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);

/** A phone call, in the chat's timeline where it happened: summary first, transcript on demand. */
function CallCard({ c }: { c: CallRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex justify-center">
      <div className="w-full max-w-[560px] rounded-xl bg-white p-3.5 shadow-card">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-signal-l text-signal">
            <PhoneCall size={15} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-ink">
              {c.direction === "inbound" ? "Incoming call" : "Outgoing call"}
              {c.seconds != null && <span className="tabular text-dusk"> · {dur(c.seconds)}</span>}
            </div>
            <div className="text-xs text-stone">
              {fmtTime(c.started_at)} · {c.status}
              {c.end_reason ? ` · ${c.end_reason.replace(/_/g, " ")}` : ""}
            </div>
          </div>
          {c.transcript.length > 0 && (
            <button onClick={() => setOpen(!open)} className="text-[12.5px] font-medium text-signal hover:text-signal-d">
              {open ? "Hide transcript" : "Transcript"}
            </button>
          )}
        </div>
        {c.summary && <div className="mt-2.5 rounded-lg bg-parchment px-3 py-2 text-[13px] text-soot">{c.summary}</div>}
        {open && (
          <div className="mt-2.5 max-h-72 space-y-1.5 overflow-y-auto border-t border-sand pt-2.5">
            {c.transcript.map((t, i) => (
              <div key={i} className="text-[13px]">
                <span className={cx("mr-1.5 text-[11px] font-semibold uppercase", t.role === "agent" ? "text-signal" : "text-dusk")}>
                  {t.role === "agent" ? "AI" : "Caller"}
                </span>
                <span className="text-soot">{plain(t.text ?? "")}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Who this is across every channel, what they ordered, and what the AI remembers about them. */
function CustomerPanel({ customerId, businessId }: { customerId: string; businessId: string }) {
  const q = useQuery({
    queryKey: ["customer", businessId, customerId],
    queryFn: () => api.get<CustomerView>(`/customers/${customerId}`, { business_id: businessId }),
    refetchInterval: 10000,
  });
  const c = q.data;
  if (q.error)
    return (
      <div className="p-4">
        <ErrorNote error={q.error} />
      </div>
    );
  if (!c)
    return (
      <div className="p-4">
        <Skeleton lines={6} />
      </div>
    );
  return (
    <div className="space-y-5 p-4">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-signal-l text-sm font-semibold text-signal-d">{initials(c.name)}</div>
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-ink">{c.name ?? "Unknown"}</div>
          <div className="text-xs text-stone">
            {c.phone ?? "-"}
            {c.language ? ` · ${c.language}` : ""}
          </div>
        </div>
      </div>
      <div>
        <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-stone">Reaches us on</div>
        <ul className="space-y-1.5">
          {c.identities.map((i, n) => (
            <li key={n} className="flex items-center gap-2 text-[13px]">
              <ChannelIcon channel={i.channel} size="sm" />
              <span className="text-ink">{channelLabel(i.channel)}</span>
              <span className="ml-auto text-[11px] text-stone">{LINKED[i.linked_via] ?? i.linked_via}</span>
            </li>
          ))}
        </ul>
        {c.merged_from.length > 0 && <div className="mt-1.5 text-xs text-stone">One customer across channels, histories merged.</div>}
      </div>
      <div>
        <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-stone">Orders · {c.orders.length}</div>
        {c.orders.length ? (
          <ul className="space-y-1">
            {c.orders.slice(0, 6).map((o) => (
              <li key={o.id}>
                <Link to={`/orders?o=${o.id}`} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-[13px] hover:bg-bone">
                  <span className="font-medium text-ink">{o.order_number}</span>
                  <StatusBadge status={o.status} />
                  <span className="tabular ml-auto text-dusk">{inr(o.total_rupees)}</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-xs text-stone">No orders yet.</div>
        )}
      </div>
      {(c.summary || c.facts.length > 0) && (
        <div>
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-stone">The AI remembers</div>
          {c.summary && <p className="text-[13px] text-soot">{c.summary}</p>}
          <ul className="mt-1 space-y-1">
            {c.facts.map((f, n) => (
              <li key={n} className="text-[13px] text-dusk">
                · {typeof f === "string" ? f : (f.text ?? f.fact ?? JSON.stringify(f))}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ThreadView({ id, businessId, calls }: { id: string; businessId: string; calls: CallRow[] }) {
  const [panel, setPanel] = useState(() => window.innerWidth >= 1500);
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const thread = useQuery({
    queryKey: ["thread", businessId, id],
    queryFn: () => api.get<Thread>(`/conversations/${id}`, { business_id: businessId }),
    refetchInterval: 4000,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["thread", businessId, id] });
    void qc.invalidateQueries({ queryKey: ["conversations", businessId] });
  };
  const reply = useMutation({
    mutationFn: (body: string) => api.post(`/conversations/${id}/reply`, { text: body }, { business_id: businessId }),
    onSuccess: () => {
      setText("");
      refresh();
    },
  });
  const takeover = useMutation({
    mutationFn: (on: boolean) => api.post(`/conversations/${id}/${on ? "takeover" : "release"}`, {}, { business_id: businessId }),
    onSuccess: refresh,
  });
  // a block body: scrollIntoView returns a Promise in current Chromium, and an effect must return nothing or a cleanup
  // function - returning the Promise made React call it on unmount and blanked the panel ("c is not a function")
  // (and scroll the message list only - scrollIntoView also moved the whole page)
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [thread.data?.messages.length]);

  const t = thread.data;
  if (thread.error) return <ErrorNote error={thread.error} />;
  if (!t)
    return (
      <div className="p-6">
        <Skeleton lines={8} />
      </div>
    );
  const humanActive = !!t.human_active_until && new Date(t.human_active_until) > new Date();
  // calls sit in the chat where they happened (a call is this customer talking on another line)
  const timeline: ({ kind: "msg"; at: string; m: ThreadMessage } | { kind: "call"; at: string; c: CallRow })[] = [
    ...t.messages.map((m) => ({ kind: "msg" as const, at: m.at ?? "", m })),
    ...calls.map((c) => ({ kind: "call" as const, at: c.started_at ?? "", c })),
  ].sort((x, y) => x.at.localeCompare(y.at));
  const name = t.customer?.name ?? t.customer?.phone ?? t.role;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim()) reply.mutate(text.trim());
  };

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex items-center gap-3 border-b border-sand px-5 py-3.5">
        <ChannelIcon channel={t.channel} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold text-ink">{name}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-stone">
            <span>{channelLabel(t.channel)}</span>
            {t.customer?.phone && t.customer.name && <span>· {t.customer.phone}</span>}
            {t.customer?.language && <Badge>{t.customer.language}</Badge>}
            {t.needs_attention && (
              <Badge tone="bad" dot>
                Needs attention
              </Badge>
            )}
            {t.simulated && <Badge tone="info">Simulated</Badge>}
          </div>
        </div>
        {t.customer && (
          <Button variant="subtle" onClick={() => setPanel(!panel)} title={panel ? "Hide customer" : "Show customer"}>
            {panel ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
          </Button>
        )}
        {humanActive ? (
          <Button variant="ghost" onClick={() => takeover.mutate(false)} disabled={takeover.isPending}>
            <UserCheck size={14} /> Hand back to AI
          </Button>
        ) : (
          <Button variant="ghost" onClick={() => takeover.mutate(true)} disabled={takeover.isPending}>
            <Hand size={14} /> Take over
          </Button>
        )}
      </div>
      {humanActive && (
        <div className="flex items-center gap-2 border-b border-[#f6e1ae] bg-sun-l px-5 py-2 text-[12.5px] text-[#8a4b00]">
          <Hand size={13} /> A person is handling this chat. The AI stays quiet until {fmtTime(t.human_active_until)}.
        </div>
      )}
      {t.customer?.facts.length ? (
        <div className="flex flex-wrap gap-1 border-b border-sand px-5 py-2">
          {t.customer.facts.map((f) => (
            <Badge key={f.key} tone="accent" title={f.source}>
              {f.key}: {String(f.value)}
            </Badge>
          ))}
        </div>
      ) : null}
      <div ref={scroller} className="min-h-0 flex-1 space-y-2.5 overflow-y-auto overscroll-contain bg-parchment/60 px-5 py-4">
        {timeline.map((e) => (e.kind === "call" ? <CallCard key={e.c.id} c={e.c} /> : <Bubble key={e.m.id} m={e.m} />))}
      </div>
      <div className="border-t border-sand p-3">
        <ErrorNote error={reply.error} />
        <form onSubmit={submit} className="flex gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Reply as the team (this pauses the AI for this chat)"
            className="h-9 flex-1 rounded-md bg-white px-3 text-[13.5px] text-ink shadow-sm ring-1 ring-inset ring-sand placeholder:text-stone focus:outline-none focus:ring-2 focus:ring-signal/50"
          />
          <Button type="submit" disabled={reply.isPending || !text.trim()} className="h-9">
            <Send size={14} /> Send
          </Button>
        </form>
      </div>
      </div>
      {panel && t.customer && (
        <aside className="w-[300px] shrink-0 overflow-y-auto border-l border-sand">
          <CustomerPanel customerId={t.customer.id} businessId={businessId} />
        </aside>
      )}
    </div>
  );
}

type ListFilter = "all" | "attention" | "calls";

export function Inbox() {
  const { business } = useSession();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState("");
  const selected = params.get("c");
  const { events } = useXrayStream(business?.id, 50);
  const list = useQuery({
    queryKey: ["conversations", business?.id],
    queryFn: () => api.get<{ items: ConversationItem[] }>("/conversations", { business_id: business!.id }),
    enabled: !!business,
    refetchInterval: 10000,
  });
  useEffect(() => {
    if (events.length) void qc.invalidateQueries({ queryKey: ["conversations", business?.id] });
  }, [events.length, qc, business?.id]);
  const callList = useQuery({
    queryKey: ["calls", business?.id],
    queryFn: () => api.get<CallRow[]>("/calls", { business_id: business!.id }),
    enabled: !!business,
    refetchInterval: 10000,
  });
  const [show, setShow] = useState<ListFilter>("all");
  const [hideLab, setHideLab] = useState(false);
  const callsByConv = useMemo(() => {
    const m = new Map<string, CallRow[]>();
    for (const c of callList.data ?? []) if (c.conversation_id) m.set(c.conversation_id, [...(m.get(c.conversation_id) ?? []), c]);
    return m;
  }, [callList.data]);
  const items = useMemo(() => {
    let all = list.data?.items ?? [];
    if (hideLab) all = all.filter((c) => !c.simulated);
    if (show === "attention") all = all.filter((c) => c.needs_attention || c.human_active);
    if (show === "calls") all = all.filter((c) => callsByConv.has(c.id));
    const f = filter.trim().toLowerCase();
    return f ? all.filter((c) => `${c.customer ?? ""} ${c.last_text}`.toLowerCase().includes(f)) : all;
  }, [list.data, filter, show, hideLab, callsByConv]);
  const attention = (list.data?.items ?? []).filter((c) => c.needs_attention || c.human_active).length;

  return (
    <div className="grid h-[calc(100vh-112px)] grid-cols-[340px_minmax(0,1fr)] gap-5">
      <Card flush className="flex min-h-0 flex-col overflow-hidden">
        <div className="border-b border-sand p-3">
          <div className="mb-2.5 flex items-center justify-between px-1">
            <h2 className="text-[15px] font-semibold text-ink">Conversations</h2>
            {list.data && <span className="tabular text-xs text-stone">{list.data.items.length}</span>}
          </div>
          <div className="mb-2.5">
            <Segmented<ListFilter>
              value={show}
              onChange={setShow}
              options={[
                { value: "all", label: "All" },
                {
                  value: "attention",
                  label: (
                    <span>
                      Needs you{attention ? <span className="tabular ml-1 text-ember">{attention}</span> : null}
                    </span>
                  ),
                },
                {
                  value: "calls",
                  label: (
                    <span className="inline-flex items-center gap-1">
                      <PhoneCall size={12} /> Calls
                    </span>
                  ),
                },
              ]}
            />
          </div>
          <label className="relative block">
            <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-stone" />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Search name or message"
              className="h-8 w-full rounded-md bg-parchment pl-8 pr-3 text-[13px] text-ink ring-1 ring-inset ring-sand placeholder:text-stone focus:bg-white focus:outline-none focus:ring-2 focus:ring-signal/50"
            />
          </label>
          <label className="mt-2 flex items-center gap-2 px-1 text-xs text-dusk">
            <input type="checkbox" checked={hideLab} onChange={(e) => setHideLab(e.target.checked)} className="accent-signal" />
            Hide Simulation Lab chats
          </label>
        </div>
        <ErrorNote error={list.error} />
        <ul className="min-h-0 flex-1 divide-y divide-sand overflow-y-auto overscroll-contain">
          {!list.data && (
            <li className="p-4">
              <Skeleton lines={6} />
            </li>
          )}
          {list.data && !items.length && (
            <li>
              <Empty icon={MessagesSquare}>{filter ? "No matching conversations" : "No conversations yet"}</Empty>
            </li>
          )}
          {items.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => setParams({ c: c.id })}
                className={cx("flex w-full gap-3 px-4 py-3 text-left transition-colors", c.id === selected ? "bg-signal-l/70" : "hover:bg-bone/70")}
              >
                <div className="relative shrink-0">
                  <div className="flex h-9 w-9 items-center justify-center rounded-full bg-bone text-xs font-semibold text-dusk">
                    {initials(c.customer ?? c.role)}
                  </div>
                  <ChannelIcon channel={c.channel} size="sm" className="absolute -bottom-1 -right-1 !h-4 !w-4 ring-2 ring-white" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="flex-1 truncate text-[13.5px] font-medium text-ink">{c.customer ?? c.role}</span>
                    {callsByConv.has(c.id) && <PhoneCall size={12} className="shrink-0 text-signal" />}
                    <span className="shrink-0 text-[11px] text-stone">{fmtWhen(c.last_at)}</span>
                  </div>
                  <div className="mt-0.5 truncate text-[12.5px] text-dusk">
                    {c.last_direction === "outbound" && <span className="text-stone">You: </span>}
                    {plain(c.last_text ?? "")}
                  </div>
                  {(c.needs_attention || c.human_active || c.simulated) && (
                    <div className="mt-1.5 flex gap-1">
                      {c.needs_attention && (
                        <Badge tone="bad" dot>
                          Needs attention
                        </Badge>
                      )}
                      {c.human_active && <Badge tone="warn">Person handling</Badge>}
                      {c.simulated && !business?.sandbox && <Badge tone="info">Simulated</Badge>}
                    </div>
                  )}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </Card>
      <Card flush className="min-h-0 overflow-hidden">
        {selected && business ? (
          <ThreadView id={selected} businessId={business.id} calls={callsByConv.get(selected) ?? []} />
        ) : (
          <div className="flex flex-1 items-center justify-center">
            <Empty icon={UserRound} hint="Every channel in one place: chats, calls and the customer behind them.">
              Select a conversation
            </Empty>
          </div>
        )}
      </Card>
    </div>
  );
}
