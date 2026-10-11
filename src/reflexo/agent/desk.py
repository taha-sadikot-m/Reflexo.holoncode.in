"""Desk runner (TurnRunnerPort, POC_04 §4.2 finalize/fallback, §7 steps 1–3).

Runs the customer graph, then performs the turn's effects atomically in `TurnHandle.commit_reply`: outbound
message(s), owner question, customer facts and the `agent_runs` row. Notifications go out after the commit.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent import language as lang
from reflexo.agent import prompts
from reflexo.agent.checkpointer import get_checkpointer
from reflexo.agent.context import PreparedContext, holding_text, prepare
from reflexo.agent.graphs.customer import CustomerState, TurnDeps, build_graph
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.questions import SLA_QUEUE
from reflexo.agent.runs import record_run
from reflexo.agent.schemas import AgentOutput, OwnerQuestionDraft
from reflexo.agent.tools.base import ToolContext
from reflexo.agent.tools.customer import CUSTOMER_TOOLS
from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer, OwnerNotifyPort, TurnHandle, TurnRef
from reflexo.common.db import get_db
from reflexo.common.enums import Desk, PipelineEventStatus, PriorityClass, TurnOutcome
from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx, current_call_ctx
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.timeutil import utcnow
from reflexo.common.xray import emit

log = get_logger(__name__)

HANDOFF_ACK = {
    "hi-Latn": "Main abhi team ko bata rahi hoon 🙏 Woh aapse jaldi baat karenge.",
    "hi": "मैं अभी टीम को बता रही हूँ 🙏 वे जल्दी आपसे बात करेंगे।",
    "en": "I'm letting the team know right away 🙏 They'll get back to you shortly.",
}
STILL_CHECKING = {
    "hi-Latn": "Main abhi bhi team se confirm kar rahi hoon 🙏 Jaise hi jawab aata hai, batati hoon.",
    "hi": "मैं अभी भी टीम से पूछ रही हूँ 🙏 जवाब आते ही बताती हूँ।",
    "en": "I'm still checking with the team 🙏 I'll get back to you as soon as I hear from them.",
}
TIER = {"send": "t0", "no_reply": "t0", "ask_owner": "t1", "fallback": "t1", "handoff": "t2"}
OUTCOME = {
    "send": TurnOutcome.REPLIED,
    "handoff": TurnOutcome.HANDOFF,
    "ask_owner": TurnOutcome.ASKED_OWNER,
    "fallback": TurnOutcome.FALLBACK,
    "no_reply": TurnOutcome.NO_REPLY,
}


async def _short_code(s: AsyncSession, business_id: UUID) -> str:
    taken = set(
        (
            await s.execute(
                text("SELECT short_code FROM owner_questions WHERE business_id = :b AND status = 'open'"),
                {"b": business_id},
            )
        ).scalars()
    )
    for _ in range(50):
        code = f"{secrets.randbelow(9000) + 1000}"
        if code not in taken:
            return code
    raise RuntimeError("no free owner-question short code")


async def create_owner_question(
    s: AsyncSession,
    *,
    business_id: UUID,
    conversation_id: UUID,
    customer_id: UUID,
    turn_id: UUID | None,
    draft: OwnerQuestionDraft,
    sla_min: int,
) -> tuple[UUID, str]:
    qid, code = new_id(), await _short_code(s, business_id)
    await s.execute(
        text("""INSERT INTO owner_questions (id, business_id, conversation_id, customer_id, source_turn_id, question,
                                             ai_guess, options, short_code, asked_at, sla_due_at)
                VALUES (:id, :b, :c, :cust, :t, :q, :g, CAST(:o AS jsonb), :code, now(), :due)"""),
        {
            "id": qid,
            "b": business_id,
            "c": conversation_id,
            "cust": customer_id,
            "t": turn_id,
            "q": draft.question[:1000],
            "g": draft.ai_guess,
            "o": json.dumps(draft.options, ensure_ascii=False),
            "code": code,
            "due": utcnow() + timedelta(minutes=sla_min),
        },
    )
    return qid, code


class CustomerDesk:
    def __init__(self, registry: Registry, model_gateway: ModelGateway) -> None:
        self.registry = registry
        self.gateway = model_gateway
        self._graph: CompiledStateGraph[CustomerState] | None = None

    async def graph(self) -> CompiledStateGraph[CustomerState]:
        if self._graph is None:
            self._graph = build_graph().compile(checkpointer=await get_checkpointer())
        return self._graph

    # ---------------------------------------------------------------- TurnRunnerPort
    async def run_turn(self, handle: TurnHandle) -> None:
        ref = handle.ref
        if ref.desk != Desk.CUSTOMER:
            log.info("desk_not_available_yet", desk=str(ref.desk), turn_id=str(ref.turn_id))  # owner desk: WP-10
            await handle.finish(TurnOutcome.NO_REPLY)
            return
        started = time.perf_counter()
        ctx = CallCtx(
            business_id=ref.business_id,
            purpose="customer_turn",
            turn_id=ref.turn_id,
            conversation_id=ref.conversation_id,
        )
        token = current_call_ctx.set(ctx)
        try:
            prepared = await prepare(ref)
            await emit(
                "context",
                PipelineEventStatus.OK,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                turn_id=ref.turn_id,
                duration_ms=prepared.timings_ms["prepare"],
                speculative_ms=prepared.timings_ms["speculative"],
                cards=len(prepared.speculative_cards),
                language=prepared.language_guess,
            )
            customer = prepared.conversation.customer
            if customer is None:
                await handle.finish(TurnOutcome.NO_REPLY)
                return
            bound = [
                t
                for t in CUSTOMER_TOOLS
                if not (t == "offer_whatsapp_continue" and prepared.conversation.channel == "whatsapp")
            ]
            deps = TurnDeps(
                handle=handle,
                prepared=prepared,
                tool_ctx=ToolContext(
                    business_id=ref.business_id,
                    desk="customer",
                    channel=prepared.conversation.channel,
                    conversation_id=ref.conversation_id,
                    customer_id=customer.id,
                    member_id=None,
                    turn_id=ref.turn_id,
                ),
                gateway=self.gateway,
                bound=bound,
                wa_display_number=get_settings().wa_display_number or None,
            )
            graph = await self.graph()
            config: RunnableConfig = {
                "configurable": {"thread_id": str(ref.turn_id), "deps": deps},
                "recursion_limit": 40,
            }
            snapshot = await graph.aget_state(config)
            resume = bool(snapshot.next)  # crashed mid-turn: continue from the last checkpoint
            graph_started = time.perf_counter()
            start: CustomerState | None = None if resume else CustomerState()
            final = cast(CustomerState, await graph.ainvoke(start, config, durability="async"))
            graph_ms = int((time.perf_counter() - graph_started) * 1000)
        finally:
            current_call_ctx.reset(token)
        await self._finalize(handle, prepared, final, ctx, started, graph_ms, resumed=resume)

    async def _finalize(
        self,
        handle: TurnHandle,
        prepared: PreparedContext,
        final: CustomerState,
        ctx: CallCtx,
        started: float,
        graph_ms: int,
        *,
        resumed: bool,
    ) -> None:
        ref = handle.ref
        conv = prepared.conversation
        customer = conv.customer
        assert customer is not None
        output = AgentOutput.model_validate(final["output"]) if final.get("output") else None
        gate = final.get("gate") or None
        action: str = output.action if output else "fallback"
        if final.get("result") == "fallback":
            action = "fallback"
        elif gate and gate.get("rewritten_action"):
            action = str(gate["rewritten_action"])
        language = output.language if output else prepared.language_guess
        settings = prepared.settings

        blocks: list[Any] = []
        draft: OwnerQuestionDraft | None = None
        if action == "send" and output:
            blocks = list(output.blocks)
        elif action == "handoff":
            originally_handoff = output is not None and output.action == "handoff" and output.blocks
            blocks = (
                list(output.blocks)
                if originally_handoff and output
                else [{"type": "text", "text": HANDOFF_ACK.get(language, HANDOFF_ACK["hi-Latn"])}]
            )
        elif action in ("fallback", "ask_owner") and prepared.relay:
            # a relay turn that could not compose: the owner's own words are the answer (never re-ask the owner)
            blocks = [{"type": "text", "text": str(prepared.relay["answer"])}]
            action = "send"
        elif action == "fallback" and prepared.open_question:
            blocks = [{"type": "text", "text": STILL_CHECKING.get(language, STILL_CHECKING["hi-Latn"])}]
        elif action in ("ask_owner", "fallback"):
            blocks = [{"type": "text", "text": holding_text(settings, language)}]
            draft = (output.owner_question if output and action == "ask_owner" else None) or OwnerQuestionDraft(
                question=prepared.inbound_text[:500] or "(customer sent a message without text)"
            )
        outcome = OUTCOME[action]
        sla = int(settings.get("owner_question_sla_min") or 15)
        question: tuple[UUID, str] | None = None
        gate_summary = {k: gate.get(k) for k in ("passed", "rewritten_action")} if gate else None
        if gate:
            gate_summary = gate_summary or {}
            gate_summary["failed"] = [c["name"] for c in gate.get("checks", []) if not c.get("passed")]
        breakdown = {
            "prepare": prepared.timings_ms["prepare"],
            "speculative": prepared.timings_ms["speculative"],
            "graph": graph_ms,
            "resumed": resumed,
            "fallback_reason": final.get("fallback_reason"),
        }

        async def write(s: AsyncSession) -> None:
            nonlocal question
            if blocks:
                await gateway.outbound().enqueue(
                    s,
                    business_id=ref.business_id,
                    conversation_id=ref.conversation_id,
                    blocks=blocks,
                    origin="ai_turn",
                    idempotency_base=f"turn:{ref.turn_id}",
                    turn_id=ref.turn_id,
                )
            if action == "handoff":
                await gateway.mark_needs_attention(s, ref.conversation_id)
            if draft is not None:
                question = await create_owner_question(
                    s,
                    business_id=ref.business_id,
                    conversation_id=ref.conversation_id,
                    customer_id=customer.id,
                    turn_id=ref.turn_id,
                    draft=draft,
                    sla_min=sla,
                )
                await self._schedule_sla(s, ref.business_id, question[0], sla)
            if prepared.relay and blocks:
                await s.execute(
                    text("""UPDATE owner_questions SET status = 'relayed', relayed_at = now()
                            WHERE id = CAST(:q AS uuid) AND status = 'answered'"""),
                    {"q": prepared.relay["question_id"]},
                )
            if output and output.customer_facts:
                await gateway.add_customer_facts(
                    s, customer.id, [f.model_dump() for f in output.customer_facts], source="customer_chat"
                )
            if language != customer.language:
                await gateway.set_customer_language(s, customer.id, language)
            await record_run(
                s,
                ctx,
                desk="customer",
                prompt_version=prompts.prompt_version("customer"),
                model_primary=self.gateway.config.role("customer_desk").primary,
                latency_ms=int((time.perf_counter() - started) * 1000),
                latency_breakdown=breakdown,
                outcome=str(outcome),
                topic=output.topic if output else None,
                tier=TIER[action],
                grounded=bool(output and output.referenced_records and gate and gate.get("passed")),
                gate_result=gate_summary,
                error={"reason": final.get("fallback_reason")} if action == "fallback" else None,
            )

        if action == "no_reply":
            async with get_db().tenant_session(ref.business_id) as s:
                await write(s)
            await handle.finish(TurnOutcome.NO_REPLY)
        else:
            await handle.commit_reply(write, outcome)
        await emit(
            "reply",
            PipelineEventStatus.OK,
            business_id=ref.business_id,
            conversation_id=ref.conversation_id,
            turn_id=ref.turn_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            action=action,
            topic=output.topic if output else None,
            model_calls=ctx.stats.model_calls,
            hedged=ctx.stats.hedged,
        )
        await self._notify(ref.business_id, action, conv, customer, prepared.inbound_text, draft, question, prepared)

    async def _notify(
        self,
        business_id: UUID,
        action: str,
        conv: gateway.ConversationView,
        customer: gateway.CustomerView,
        inbound_text: str,
        draft: OwnerQuestionDraft | None,
        question: tuple[UUID, str] | None,
        prepared: PreparedContext | None,
    ) -> None:
        notifier = self.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
        if notifier is None or action not in ("handoff", "ask_owner", "fallback"):
            return
        context = owner_context(conv, customer, prepared)
        if question and draft:
            qid, code = question
            guess = f"\nMy guess: {draft.ai_guess}" if draft.ai_guess else ""
            why = "I couldn't handle this message myself." if action == "fallback" else "I don't have this answer."
            body = f'❓ *Question {code}* — {why}\n"{draft.question}"{guess}\n\n{context}\n\nReply: `{code} <answer>`'
            buttons = [(f"oq:{qid}:{i}", str(opt)[:20]) for i, opt in enumerate(draft.options[:2])]
            buttons.append((f"oq:{qid}:later", "I'll reply"))
            await notifier.notify(
                business_id,
                "question",
                body,
                dedupe_key=f"oq:{qid}",
                priority=PriorityClass.P1,
                buttons=buttons,
                ref={"question_id": str(qid)},
            )
        elif action == "fallback" and prepared and prepared.open_question:
            code = prepared.open_question["code"]
            body = f'🔁 *Follow-up on question {code}*: "{inbound_text[:300]}"\n\n{context}'
            await notifier.notify(
                business_id,
                "question_followup",
                body,
                dedupe_key=f"oqf:{conv.id}:{int(time.time() // 600)}",
                priority=PriorityClass.P1,
            )
        else:
            body = f"🙋 *Customer needs you* — please reply to them on {conv.channel.replace('_', ' ')}.\n\n{context}"
            await notifier.notify(
                business_id,
                "handoff",
                body,
                dedupe_key=f"handoff:{conv.id}:{int(time.time() // 600)}",
                priority=PriorityClass.P1,
            )

    async def _schedule_sla(self, s: AsyncSession, business_id: UUID, question_id: UUID, sla_min: int) -> None:
        await self.registry.require(JobEnqueuer).enqueue(  # type: ignore[type-abstract]
            SLA_QUEUE,
            {"business_id": str(business_id), "question_id": str(question_id)},
            business_id=business_id,
            priority=PriorityClass.P2,
            dedupe_key=f"oqsla:{question_id}",
            delay_s=sla_min * 60,
            session=s,
        )

    async def send_holding_reply(self, business_id: UUID, conversation_id: UUID, turn_id: UUID) -> None:
        settings = await knowledge.business_settings(business_id)
        async with get_db().tenant_session(business_id) as s:
            recent = await gateway.recent_messages(s, conversation_id, limit=3)
            last_in = next((m.text for m in reversed(recent) if m.direction == "inbound"), "")
            text_ = holding_text(settings, lang.detect(last_in, str(settings.get("default_language") or "hi-Latn")))
            await gateway.outbound().enqueue(
                s,
                business_id=business_id,
                conversation_id=conversation_id,
                origin="system",
                turn_id=turn_id,
                idempotency_base=f"holding:{turn_id}",
                blocks=[{"type": "text", "text": text_}],
            )

    async def run_fallback(self, ref: TurnRef, reason: str) -> None:
        """The turn failed permanently: holding reply + owner question with the raw customer text."""
        settings = await knowledge.business_settings(ref.business_id)
        async with get_db().tenant_session(ref.business_id) as s:
            conv = await gateway.conversation_view(s, ref.conversation_id)
            msgs = await gateway.messages_by_ids(s, list(ref.message_ids))
            if conv is None or conv.customer is None:
                return
            raw = "\n".join(m.text for m in msgs if m.text)[:500] or "(message without text)"
            language = lang.detect(raw, str(settings.get("default_language") or "hi-Latn"))
            await gateway.outbound().enqueue(
                s,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                origin="system",
                turn_id=ref.turn_id,
                idempotency_base=f"fallback:{ref.turn_id}",
                blocks=[{"type": "text", "text": holding_text(settings, language)}],
            )
            draft = OwnerQuestionDraft(question=raw)
            question = await create_owner_question(
                s,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                customer_id=conv.customer.id,
                turn_id=ref.turn_id,
                draft=draft,
                sla_min=int(settings.get("owner_question_sla_min") or 15),
            )
        log.warning("turn_fallback", turn_id=str(ref.turn_id), reason=reason)
        await self._notify(ref.business_id, "fallback", conv, conv.customer, raw, draft, question, None)


def owner_context(
    conv: gateway.ConversationView, customer: gateway.CustomerView, prepared: PreparedContext | None
) -> str:
    """What the owner needs to act without opening the panel: who, where, the open order and the last messages."""
    who = customer.display_name or "Customer"
    phone = f" ({customer.phone_e164})" if customer.phone_e164 else ""
    lines = [f"👤 {who}{phone} · {conv.channel.replace('_', ' ')}"]
    order = prepared.open_order if prepared else None
    if order:
        items = ", ".join(f"{ln['qty']} × {ln['item']}" for ln in order.get("lines", []))
        lines.append(f"🧾 Order {order['order_number']} ({order['status']}): {items} — total {order['total']}")
    if prepared and prepared.recent_lines:
        lines.append("💬 Recent:")
        lines += [f"  {line[:200]}" for line in prepared.recent_lines[-4:]]
    return "\n".join(lines)
