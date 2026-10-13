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
import { useSearchParams } from "react-router-dom";

import { fmtWhen } from "../components/bits";
import { ChannelIcon, channelLabel } from "../components/channel";
import { Badge, Button, Card, Empty, ErrorNote, Skeleton, cx } from "../components/ui";
import { api, type ConversationItem, type Thread, type ThreadMessage } from "../lib/api";
import { useSession } from "../lib/session";
import { fmtTime } from "../lib/stages";
import { useXrayStream } from "../lib/stream";

const TICKS: Record<string, { icon: LucideIcon; tone: string; title: string }> = {
  queued: { icon: Clock3, tone: "text-stone", title: "Queued" },
  sending: { icon: Clock3, tone: "text-stone", title: "Sending" },
  unconfirmed: { icon: AlertTriangle, tone: "text-sun", title: "Outcome unknown — retrying safely" },
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
          {m.text}
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

function ThreadView({ id, businessId }: { id: string; businessId: string }) {
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
  // function — returning the Promise made React call it on unmount and blanked the panel ("c is not a function")
  // (and scroll the message list only — scrollIntoView also moved the whole page)
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
  const name = t.customer?.name ?? t.customer?.phone ?? t.role;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim()) reply.mutate(text.trim());
  };

  return (
    <div className="flex h-full flex-col">
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
          <Hand size={13} /> A person is handling this chat — the AI stays quiet until {fmtTime(t.human_active_until)}.
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
      <div ref={scroller} className="flex-1 space-y-2.5 overflow-auto bg-parchment/60 px-5 py-4">
        {t.messages.map((m) => (
          <Bubble key={m.id} m={m} />
        ))}
      </div>
      <div className="border-t border-sand p-3">
        <ErrorNote error={reply.error} />
        <form onSubmit={submit} className="flex gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Reply as the team — this pauses the AI for this chat"
            className="h-9 flex-1 rounded-md bg-white px-3 text-[13.5px] text-ink shadow-sm ring-1 ring-inset ring-sand placeholder:text-stone focus:outline-none focus:ring-2 focus:ring-signal/50"
          />
          <Button type="submit" disabled={reply.isPending || !text.trim()} className="h-9">
            <Send size={14} /> Send
          </Button>
        </form>
      </div>
    </div>
  );
}

export function Conversations() {
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
  const items = useMemo(() => {
    const all = list.data?.items ?? [];
    const f = filter.trim().toLowerCase();
    return f ? all.filter((c) => `${c.customer ?? ""} ${c.last_text}`.toLowerCase().includes(f)) : all;
  }, [list.data, filter]);

  return (
    <div className="grid h-[calc(100vh-112px)] grid-cols-[340px_minmax(0,1fr)] gap-5">
      <Card flush className="flex min-h-0 flex-col overflow-hidden">
        <div className="border-b border-sand p-3">
          <div className="mb-2.5 flex items-center justify-between px-1">
            <h2 className="text-[15px] font-semibold text-ink">Conversations</h2>
            {list.data && <span className="tabular text-xs text-stone">{list.data.items.length}</span>}
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
        </div>
        <ErrorNote error={list.error} />
        <ul className="min-h-0 flex-1 divide-y divide-sand overflow-auto">
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
                    <span className="shrink-0 text-[11px] text-stone">{fmtWhen(c.last_at)}</span>
                  </div>
                  <div className="mt-0.5 truncate text-[12.5px] text-dusk">
                    {c.last_direction === "outbound" && <span className="text-stone">You: </span>}
                    {c.last_text}
                  </div>
                  {(c.needs_attention || c.human_active || c.simulated) && (
                    <div className="mt-1.5 flex gap-1">
                      {c.needs_attention && (
                        <Badge tone="bad" dot>
                          Needs attention
                        </Badge>
                      )}
                      {c.human_active && <Badge tone="warn">Person handling</Badge>}
                      {c.simulated && <Badge tone="info">Simulated</Badge>}
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
          <ThreadView id={selected} businessId={business.id} />
        ) : (
          <div className="flex h-full items-center justify-center">
            <Empty icon={MessagesSquare} hint="Every channel in one place — WhatsApp, Telegram, Messenger and Instagram.">
              Select a conversation
            </Empty>
          </div>
        )}
      </Card>
    </div>
  );
}
