"""Desk runner (TurnRunnerPort, POC_04 §4.2 finalize/fallback, §7 steps 1–3).

Runs the customer graph, then performs the turn's effects atomically in `TurnHandle.commit_reply`: outbound
message(s), owner question, customer facts and the `agent_runs` row. Notifications go out after the commit.
"""

from __future__ import annotations

import time
from typing import Any, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent import language as lang
from reflexo.agent import messages, prompts
from reflexo.agent.checkpointer import get_checkpointer
from reflexo.agent.context import PreparedContext, holding_text, prepare
from reflexo.agent.graphs.customer import CustomerState, TurnDeps, build_graph
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.owner_desk import OwnerDesk
from reflexo.agent.questions import SLA_QUEUE, contact_line, create_owner_question, notify_question
from reflexo.agent.runs import record_run
from reflexo.agent.schemas import AgentOutput, OwnerQuestionDraft
from reflexo.agent.tools.base import ToolContext
from reflexo.agent.tools.customer import CUSTOMER_TOOLS
from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer, TurnHandle, TurnRef
from reflexo.common.db import get_db
from reflexo.common.enums import Desk, PipelineEventStatus, PriorityClass, TurnOutcome
from reflexo.common.llm import CallCtx, current_call_ctx
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.xray import emit

log = get_logger(__name__)

HANDOFF_ACK = {
    "hi-Latn": "Main abhi team ko bata rahi hoon 🙏 Woh aapse jaldi baat karenge.",
    "hi": "मैं अभी टीम को बता रही हूँ 🙏 वे जल्दी आपसे बात करेंगे।",
    "en": "I'm letting the team know right away 🙏 They'll get back to you shortly.",
}
TIER = {"send": "t0", "no_reply": "t0", "ask_owner": "t1", "fallback": "t1", "handoff": "t2"}
OUTCOME = {
    "send": TurnOutcome.REPLIED,
    "handoff": TurnOutcome.HANDOFF,
    "ask_owner": TurnOutcome.ASKED_OWNER,
    "fallback": TurnOutcome.FALLBACK,
    "no_reply": TurnOutcome.NO_REPLY,
}


class CustomerDesk:
    def __init__(self, registry: Registry, model_gateway: ModelGateway) -> None:
        self.registry = registry
        self.gateway = model_gateway
        self._graph: CompiledStateGraph[CustomerState] | None = None
        self.owner = OwnerDesk(model_gateway)

    async def graph(self) -> CompiledStateGraph[CustomerState]:
        if self._graph is None:
            self._graph = build_graph().compile(checkpointer=await get_checkpointer())
        return self._graph

    # ---------------------------------------------------------------- TurnRunnerPort
    async def run_turn(self, handle: TurnHandle) -> None:
        ref = handle.ref
        if ref.desk == Desk.OWNER:
            await self.owner.run_turn(handle)
            return
        if ref.desk != Desk.CUSTOMER:
            log.info("desk_not_available", desk=str(ref.desk), turn_id=str(ref.turn_id))
            await handle.finish(TurnOutcome.NO_REPLY)
            return
        started = time.perf_counter()
        ctx = CallCtx(
            business_id=ref.business_id,
            purpose="customer_turn",
            turn_id=ref.turn_id,
            conversation_id=ref.conversation_id,
            prompt_version=prompts.prompt_version("customer"),
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
        # our own words (holding, hand-off, still-checking) follow the platform policy, not what the model declared
        language = lang.reply_language(None, output.language if output else prepared.language_guess)
        settings = prepared.settings

        blocks: list[Any] = []
        draft: OwnerQuestionDraft | None = None
        idempotency_base = f"turn:{ref.turn_id}"
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
            holding = await messages.holding_line(ref.business_id, ref.conversation_id, language, still_checking=True)
            if holding:
                blocks, idempotency_base = [{"type": "text", "text": holding[0]}], holding[1]
        elif action in ("ask_owner", "fallback"):
            if action == "fallback":
                holding = await messages.holding_line(ref.business_id, ref.conversation_id, language)
                if holding:
                    blocks, idempotency_base = [{"type": "text", "text": holding[0]}], holding[1]
            else:
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
                    idempotency_base=idempotency_base,
                    turn_id=ref.turn_id,
                )
            if action == "handoff":
                await gateway.mark_needs_attention(s, ref.conversation_id)
            if prepared.notice and action == "send" and blocks:  # the gate held this reply to carry the notice
                await s.execute(
                    text("UPDATE customers SET notice_shown_at = now() WHERE id = :c AND notice_shown_at IS NULL"),
                    {"c": customer.id},
                )
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
            language=output.language if output else None,
            refs=sorted({r.type for r in output.referenced_records}) if output else [],
            model_calls=ctx.stats.model_calls,
            hedged=ctx.stats.hedged,
        )
        await self._notify(
            ref.business_id,
            action,
            conv,
            customer,
            prepared.inbound_text,
            draft,
            question,
            prepared,
            brief=output.owner_brief if output else None,
        )

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
        *,
        brief: str | None = None,
    ) -> None:
        """Owner messages read like a colleague (POC_10 §8): what is needed, the agent's brief, one compact facts
        line — no transcript; [💬 Show chat] fetches the last messages only if the owner wants them."""
        if action not in ("handoff", "ask_owner", "fallback"):
            return
        contact = contact_line(conv, customer)
        open_order = order_line(prepared)
        name = customer.display_name or None
        async with get_db().tenant_session(business_id) as s:  # the owner sees what the customer showed
            media = await gateway.recent_customer_media(s, conv.id)
        if question and draft:
            qid, code = question
            await notify_question(
                business_id,
                qid=qid,
                code=code,
                question=draft.question,
                conv=conv,
                contact=contact,
                customer_wrote=inbound_text[:300] if action == "fallback" else None,
                brief=brief,
                guess=draft.ai_guess,
                options=draft.options,
                open_order=open_order,
                could_not_reply=action == "fallback",
                media=media,
            )
        elif action == "fallback" and prepared and prepared.open_question:
            await messages.notify_owner(
                "owner.question_followup",
                business_id=business_id,
                facts={
                    "customer.name": name,
                    "code": prepared.open_question["code"],
                    "customer_wrote": inbound_text[:300],
                    "contact": contact,
                    "open_order": open_order,
                },
                kind="question_followup",
                dedupe_key=f"oqf:{conv.id}:{int(time.time() // 600)}",
                about_conversation=conv.id,
                priority=PriorityClass.P1,
                buttons={"show_chat": f"chat:{conv.id}"},
            )
        else:
            await messages.notify_owner(
                "owner.handoff",
                business_id=business_id,
                facts={
                    "customer.name": name,
                    "channel": conv.channel.replace("_", " "),
                    "customer_wrote": inbound_text[:300],
                    "note": brief.strip() if brief and brief.strip() else None,
                    "contact": contact,
                    "open_order": open_order,
                },
                kind="handoff",
                dedupe_key=f"handoff:{conv.id}:{int(time.time() // 600)}",
                about_conversation=conv.id,
                priority=PriorityClass.P1,
                buttons={"show_chat": f"chat:{conv.id}"},
                media=media,
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
            language = lang.reply_language(last_in, str(settings.get("default_language") or "hi-Latn"))
            text_ = holding_text(settings, language)
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
        if ref.desk == Desk.OWNER:
            log.warning("owner_turn_fallback", turn_id=str(ref.turn_id), reason=reason)
            await self.owner.run_fallback(ref)
            return
        settings = await knowledge.business_settings(ref.business_id)
        async with get_db().tenant_session(ref.business_id) as s:
            conv = await gateway.conversation_view(s, ref.conversation_id)
            msgs = await gateway.messages_by_ids(s, list(ref.message_ids))
            if conv is None or conv.customer is None:
                return
            raw = "\n".join(m.text for m in msgs if m.text)[:500] or "(message without text)"
            language = lang.reply_language(raw, str(settings.get("default_language") or "hi-Latn"))
            holding = await messages.holding_line(ref.business_id, ref.conversation_id, language)
            if holding:
                await gateway.outbound().enqueue(
                    s,
                    business_id=ref.business_id,
                    conversation_id=ref.conversation_id,
                    origin="system",
                    turn_id=ref.turn_id,
                    idempotency_base=holding[1],
                    blocks=[{"type": "text", "text": holding[0]}],
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


def order_line(prepared: PreparedContext | None) -> str | None:
    """The open order in one line for the owner; the conversation itself is one tap away ([Show chat]), never
    pasted in (live: owners read the transcript dump as a log, not a message)."""
    order = prepared.open_order if prepared else None
    if not order:
        return None
    items = ", ".join(f"{ln['qty']} × {ln['item']}" for ln in order.get("lines", []))
    return f"{order['order_number']} ({str(order['status']).replace('_', ' ')}): {items} · {order['total']}"
