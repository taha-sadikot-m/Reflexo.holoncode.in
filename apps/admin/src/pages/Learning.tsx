import { useQuery } from "@tanstack/react-query";
import { ArrowRight, BookOpenCheck, CircleHelp, GraduationCap, Hourglass, MessageCircleQuestion, Scale, Sparkles } from "lucide-react";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { PageHeader, fmtWhen } from "../components/bits";
import { Badge, Card, Empty, ErrorNote, Segmented, Skeleton, Stat, cx } from "../components/ui";
import { api, type OwnerWorkspace, type PlaybookProposal, type PlaybookRuleRow } from "../lib/api";
import { useSession } from "../lib/session";

type Filter = "all" | "waiting" | "learned";

/** One time the AI needed the owner: what it asked, what the owner said, what it learned. */
type Entry = {
  key: string;
  at: string | null;
  kind: "question" | "decision" | "approval";
  title: string;
  detail?: string | null;
  customer?: string | null;
  conversationId?: string | null;
  waiting: boolean;
  outcome?: { tone: "good" | "bad" | "neutral" | "warn"; label: string };
  learned?: string | null;
  code?: string | null;
};

const APPROVAL_LABEL: Record<string, string> = {
  outbound_draft: "Send a message",
  automation_activation: "Turn on new automations",
  automation_step: "An automation step",
  knowledge_conflict: "Resolve a knowledge conflict",
  change_confirmation: "Apply a catalogue change",
};

const SCOPE_LEARNED: Record<string, string> = {
  global: "Saved as knowledge for every customer",
  customer: "Remembered for this customer only",
  temporary: "Used for today only",
};

const LEARN_RULE: Record<string, string> = {
  g: "Learned a rule for every customer",
  c: "Learned a rule for this customer",
  once: "Just this once — will ask again next time",
};

function entries(owner: OwnerWorkspace | undefined, proposals: PlaybookProposal[] | undefined): Entry[] {
  const out: Entry[] = [];
  for (const q of owner?.questions ?? []) {
    const waiting = q.status === "open" || q.status === "asked";
    out.push({
      key: `q:${q.id}`,
      at: q.asked_at,
      kind: "question",
      title: q.question,
      detail: q.answer ? `Owner: “${q.answer}”` : q.ai_guess ? `AI guessed: ${q.ai_guess}` : null,
      customer: q.customer,
      conversationId: q.conversation_id,
      waiting,
      code: q.code,
      outcome: waiting
        ? { tone: "warn", label: "Waiting for the owner" }
        : q.status === "expired" || q.status === "cancelled"
          ? { tone: "neutral", label: q.status === "expired" ? "No answer in time" : "Cancelled" }
          : { tone: "good", label: q.relayed_at ? "Answered · customer told" : "Answered" },
      learned: q.answer && q.scope ? SCOPE_LEARNED[q.scope] ?? null : null,
    });
  }
  for (const p of proposals ?? []) {
    const waiting = p.status === "pending";
    out.push({
      key: `p:${p.id}`,
      at: p.created_at,
      kind: "decision",
      title: `${p.owner_initiated ? "Owner asked to" : `${p.customer ?? "A customer"} wants`}: ${p.label.toLowerCase()} — ${p.summary ?? ""}`,
      detail: p.reason ? `Reason: “${p.reason}”` : null,
      customer: p.customer,
      conversationId: p.conversation_id,
      waiting,
      outcome: waiting
        ? { tone: "warn", label: "Waiting for the owner" }
        : p.status === "approved"
          ? { tone: p.done === false ? "bad" : "good", label: p.done === false ? "Approved · could not be done" : "Approved · done" }
          : p.status === "rejected"
            ? { tone: "bad", label: "Declined" }
            : { tone: "neutral", label: p.status.replace(/_/g, " ") },
      learned: p.learned ? LEARN_RULE[p.learned] ?? null : null,
    });
  }
  for (const a of owner?.approvals ?? []) {
    if (a.kind === "tool_call") continue; // shown above as decisions, with what was learned
    const waiting = a.status === "pending";
    out.push({
      key: `a:${a.id}`,
      at: a.created_at,
      kind: "approval",
      title: `${APPROVAL_LABEL[a.kind] ?? a.kind.replace(/_/g, " ")}${a.summary ? ` — ${a.summary}` : ""}`,
      waiting,
      code: a.code,
      outcome: waiting
        ? { tone: "warn", label: "Waiting for the owner" }
        : a.status === "approved"
          ? { tone: "good", label: "Approved" }
          : a.status === "rejected"
            ? { tone: "bad", label: "Declined" }
            : { tone: "neutral", label: a.status.replace(/_/g, " ") },
    });
  }
  return out.sort((x, y) => String(y.at ?? "").localeCompare(String(x.at ?? "")));
}

const KIND: Record<Entry["kind"], { icon: typeof CircleHelp; label: string; tone: string }> = {
  question: { icon: MessageCircleQuestion, label: "Question", tone: "bg-sky-l text-sky" },
  decision: { icon: Scale, label: "Decision", tone: "bg-signal-l text-signal" },
  approval: { icon: BookOpenCheck, label: "Approval", tone: "bg-bone text-dusk" },
};

export function Learning() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  const [params, setParams] = useSearchParams();
  const filter = (params.get("filter") as Filter) || "all";
  const owner = useQuery({
    queryKey: ["owner", bid],
    queryFn: () => api.get<OwnerWorkspace>("/owner", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  const book = useQuery({
    queryKey: ["playbook", bid],
    queryFn: () => api.get<{ rules: PlaybookRuleRow[]; proposals: PlaybookProposal[] }>("/playbook", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 5000,
  });
  const all = useMemo(() => entries(owner.data, book.data?.proposals), [owner.data, book.data]);
  const shown = all.filter((e) => (filter === "waiting" ? e.waiting : filter === "learned" ? !!e.learned : true));
  const waiting = all.filter((e) => e.waiting).length;
  const rules = (book.data?.rules ?? []).filter((r) => r.status === "active").length;
  const savedAnswers = all.filter((e) => e.kind === "question" && e.learned).length;

  return (
    <div className="space-y-5">
      <PageHeader title="Learning">
        Every time the AI was not sure, it asked the owner, did exactly what they decided, and offered to remember it. This is how it learns how
        this shop works.
      </PageHeader>
      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <Stat label="Times it asked" value={owner.data ? all.length : "–"} icon={CircleHelp} />
        <Stat label="Waiting for the owner" value={owner.data ? waiting : "–"} icon={Hourglass} tone={waiting ? "warn" : "good"} />
        <Stat label="Rules it follows" value={book.data ? rules : "–"} icon={GraduationCap} tone="good" hint={<Link to="/brain/rules" className="text-signal hover:text-signal-d">See the rules</Link>} />
        <Stat label="Answers it remembers" value={owner.data ? savedAnswers : "–"} icon={Sparkles} hint={<Link to="/brain/knowledge" className="text-signal hover:text-signal-d">See the knowledge</Link>} />
      </div>
      <Segmented<Filter>
        value={filter}
        onChange={(v) => setParams(v === "all" ? {} : { filter: v }, { replace: true })}
        options={[
          { value: "all", label: <span>All <span className="tabular ml-1 text-stone">{all.length}</span></span> },
          { value: "waiting", label: <span>Waiting <span className="tabular ml-1 text-stone">{waiting}</span></span> },
          { value: "learned", label: <span>Learned <span className="tabular ml-1 text-stone">{all.filter((e) => e.learned).length}</span></span> },
        ]}
      />
      <ErrorNote error={owner.error ?? book.error} />
      <Card flush>
        {!owner.data || !book.data ? (
          <div className="p-5">
            <Skeleton lines={8} />
          </div>
        ) : shown.length ? (
          <ul className="divide-y divide-sand">
            {shown.map((e) => {
              const k = KIND[e.kind];
              return (
                <li key={e.key} className="flex gap-4 px-5 py-4">
                  <span className={cx("mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg", k.tone)}>
                    <k.icon size={16} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-stone">
                      <span className="font-medium text-dusk">{k.label}</span>
                      {e.code && <span>#{e.code}</span>}
                      {e.customer && <span>· {e.customer}</span>}
                      <span className="ml-auto">{fmtWhen(e.at)}</span>
                    </div>
                    <div className="mt-1 text-[13.5px] leading-relaxed text-ink">{e.title}</div>
                    {e.detail && <div className="mt-0.5 text-[13px] text-dusk">{e.detail}</div>}
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      {e.outcome && (
                        <Badge tone={e.outcome.tone} dot>
                          {e.outcome.label}
                        </Badge>
                      )}
                      {e.learned && (
                        <span className="inline-flex items-center gap-1 text-[12.5px] font-medium text-signal-d">
                          <ArrowRight size={13} /> {e.learned}
                        </span>
                      )}
                      {e.conversationId && (
                        <Link to={`/inbox?c=${e.conversationId}`} className="ml-auto text-[12.5px] font-medium text-signal hover:text-signal-d">
                          Open chat
                        </Link>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <Empty icon={GraduationCap} hint="Questions and decisions the AI brings to the owner appear here, with what it learned.">
            {filter === "waiting" ? "Nothing waiting for the owner" : filter === "learned" ? "Nothing learned yet" : "The AI has not asked anything yet"}
          </Empty>
        )}
      </Card>
    </div>
  );
}
