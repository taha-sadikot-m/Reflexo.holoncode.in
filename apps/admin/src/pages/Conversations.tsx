import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Hand, Send, UserCheck } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { Badge, Button, Card, Empty, ErrorNote, Mono, cx } from "../components/ui";
import { api, type ConversationItem, type Thread, type ThreadMessage } from "../lib/api";
import { useSession } from "../lib/session";
import { ChannelIcon } from "../components/channel";
import { fmtTime } from "../lib/stages";
import { useXrayStream } from "../lib/stream";

const TICKS: Record<string, { mark: string; tone: string; title: string }> = {
  queued: { mark: "⏳", tone: "text-stone", title: "queued" },
  sending: { mark: "⏳", tone: "text-stone", title: "sending" },
  unconfirmed: { mark: "?", tone: "text-sun", title: "outcome unknown — retrying safely" },
  sent: { mark: "✓", tone: "text-stone", title: "sent" },
  delivered: { mark: "✓✓", tone: "text-stone", title: "delivered" },
  read: { mark: "✓✓", tone: "text-signal", title: "read" },
  failed: { mark: "✗", tone: "text-ember", title: "failed" },
  blocked: { mark: "⛔", tone: "text-ember", title: "blocked by policy" },
};

function BlockChips({ blocks }: { blocks: Record<string, unknown>[] | null }) {
  if (!blocks) return null;
  const chips: string[] = [];
  for (const b of blocks) {
    if (b.type === "product") chips.push("📦 product card");
    else if (b.type === "carousel") chips.push("🎠 carousel");
    else if (b.type === "payment_link") chips.push("💳 payment link");
    else if (b.type === "order_summary") chips.push("🧾 order summary");
    else if (b.type === "buttons" || b.type === "list")
      for (const o of (b.options as { title: string }[] | undefined) ?? []) chips.push(`▢ ${o.title}`);
  }
  if (!chips.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {chips.map((c, i) => (
        <span key={i} className="rounded-full border border-current/20 bg-white/40 px-2 py-0.5 text-[11px] dark:bg-black/20">
          {c}
        </span>
      ))}
    </div>
  );
}

function Bubble({ m }: { m: ThreadMessage }) {
  const inbound = m.direction === "inbound";
  const tick = TICKS[m.status];
  const who = inbound ? (m.author === "customer" ? "" : m.author) : m.author === "ai" ? "AI" : m.author;
  return (
    <div className={cx("flex", inbound ? "justify-start" : "justify-end")}>
      <div
        className={cx(
          "max-w-[70%] rounded-2xl px-3.5 py-2 text-sm shadow-sm",
          inbound ? "rounded-bl-sm bg-white dark:bg-twilight" : m.author === "ai" ? "rounded-br-sm bg-blossom dark:bg-[#3d2a10]" : "rounded-br-sm bg-signal-l dark:bg-[#26214d]",
          m.status === "blocked" && "opacity-60",
        )}
      >
        {who && <div className="mb-0.5 text-[10px] font-bold uppercase tracking-wide text-dusk dark:text-stone">{who}</div>}
        <div className="whitespace-pre-wrap break-words">
          {m.text}
          {m.interactive?.title && <span className="ml-1 text-xs text-dusk">[tapped “{m.interactive.title}”]</span>}
        </div>
        {m.attachments.length > 0 && <div className="mt-1 text-xs text-dusk">📎 {m.attachments.map((a) => String(a.kind ?? "file")).join(", ")}</div>}
        <BlockChips blocks={m.blocks} />
        <div className="mt-1 flex items-center justify-end gap-2 text-[10px] text-stone">
          {m.turn_id && <Mono className="!text-[10px]">turn {m.turn_id.slice(-6)}</Mono>}
          {m.error && <span className="text-ember">{String((m.error as { code?: string }).code ?? "error")}</span>}
          <span>{fmtTime(m.at)}</span>
          {!inbound && tick && (
            <span className={tick.tone} title={tick.title}>
              {tick.mark}
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
  const bottom = useRef<HTMLDivElement>(null);
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
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread.data?.messages.length]);

  const t = thread.data;
  if (thread.error) return <ErrorNote error={thread.error} />;
  if (!t) return <Empty>Loading…</Empty>;
  const humanActive = !!t.human_active_until && new Date(t.human_active_until) > new Date();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim()) reply.mutate(text.trim());
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-sand pb-3 dark:border-twilight">
        <ChannelIcon channel={t.channel} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="font-display font-semibold">{t.customer?.name ?? t.customer?.phone ?? t.role}</div>
          <div className="flex flex-wrap gap-1 text-xs">
            <Mono>{t.channel}</Mono>
            {t.customer?.language && <Badge>{t.customer.language}</Badge>}
            {t.needs_attention && <Badge tone="bad">needs attention</Badge>}
            {humanActive && <Badge tone="warn">human active</Badge>}
            {t.simulated && <Badge tone="info">simulated</Badge>}
          </div>
        </div>
        {humanActive ? (
          <Button variant="ghost" onClick={() => takeover.mutate(false)} disabled={takeover.isPending}>
            <UserCheck size={14} /> Release to AI
          </Button>
        ) : (
          <Button variant="ghost" onClick={() => takeover.mutate(true)} disabled={takeover.isPending}>
            <Hand size={14} /> Take over
          </Button>
        )}
      </div>
      {t.customer?.facts.length ? (
        <div className="flex flex-wrap gap-1 border-b border-sand py-2 text-xs dark:border-twilight">
          {t.customer.facts.map((f) => (
            <Badge key={f.key} tone="accent" title={f.source}>
              {f.key}: {String(f.value)}
            </Badge>
          ))}
        </div>
      ) : null}
      <div className="flex-1 space-y-2 overflow-auto py-4">
        {t.messages.map((m) => (
          <Bubble key={m.id} m={m} />
        ))}
        <div ref={bottom} />
      </div>
      <ErrorNote error={reply.error} />
      <form onSubmit={submit} className="flex gap-2 border-t border-sand pt-3 dark:border-twilight">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Reply as the team (pauses the AI for this chat)…"
          className="flex-1 rounded-lg border border-sand bg-parchment px-3 py-2 text-sm outline-none focus:border-marigold dark:border-twilight dark:bg-ink"
        />
        <Button type="submit" disabled={reply.isPending || !text.trim()}>
          <Send size={14} /> Send
        </Button>
      </form>
    </div>
  );
}

export function Conversations() {
  const { business } = useSession();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const selected = params.get("c");
  const { events } = useXrayStream(business?.id, 50);
  const list = useQuery({
    queryKey: ["conversations", business?.id],
    queryFn: () => api.get<{ items: ConversationItem[] }>("/conversations", { business_id: business!.id }),
    enabled: !!business,
  });
  useEffect(() => {
    if (events.length) void qc.invalidateQueries({ queryKey: ["conversations", business?.id] });
  }, [events.length, qc, business?.id]);

  return (
    <div className="grid h-full grid-cols-[360px_1fr] gap-5">
      <Card title="Conversations" className="flex min-h-0 flex-col overflow-hidden">
        <ErrorNote error={list.error} />
        {list.data && !list.data.items.length && <Empty>No conversations yet.</Empty>}
        <ul className="-mx-4 -my-4 max-h-[calc(100vh-180px)] divide-y divide-sand overflow-auto dark:divide-twilight">
          {list.data?.items.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => setParams({ c: c.id })}
                className={cx("w-full px-4 py-3 text-left hover:bg-parchment dark:hover:bg-ink", c.id === selected && "bg-blossom dark:bg-twilight")}
              >
                <div className="flex items-center gap-2">
                  <ChannelIcon channel={c.channel} size="sm" />
                  <span className="flex-1 truncate text-sm font-semibold">{c.customer ?? c.role}</span>
                  <span className="text-[11px] text-stone">{fmtTime(c.last_at)}</span>
                </div>
                <div className="mt-0.5 truncate text-xs text-dusk dark:text-stone">
                  {c.last_direction === "outbound" ? "↩ " : ""}
                  {c.last_text}
                </div>
                <div className="mt-1 flex gap-1">
                  {c.needs_attention && <Badge tone="bad">needs attention</Badge>}
                  {c.human_active && <Badge tone="warn">human</Badge>}
                  {c.simulated && <Badge tone="info">sim</Badge>}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </Card>
      <Card className="min-h-0 overflow-hidden">
        <div className="h-[calc(100vh-170px)]">{selected && business ? <ThreadView id={selected} businessId={business.id} /> : <Empty>Select a conversation.</Empty>}</div>
      </Card>
    </div>
  );
}
