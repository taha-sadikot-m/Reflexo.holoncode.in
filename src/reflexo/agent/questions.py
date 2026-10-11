"""Ask-the-owner loop, answer side (POC_04 §7 steps 4-8): scope → Q&A → relay turn → never ask twice.

The owner's answer becomes a Q&A entry through a knowledge change set (`origin='learning'`, auto-applied: the owner
is the source). The customer gets it through a *relay turn*: the customer desk answers grounded on the new
`qa:<id>`, in the customer's language. An unclear scope is saved for this customer only (never leaks to others)
and the owner is asked, with buttons, whether it applies to everyone.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.common.contracts import (
    AnswerResult,
    JobEnqueuer,
    TurnScheduler,
)
from reflexo.common.db import get_db
from reflexo.common.enums import Desk, PipelineEventStatus, PriorityClass
from reflexo.common.llm import CallCtx, ChatMessage, ModelError
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.timeutil import to_business_tz, utcnow
from reflexo.common.xray import emit

log = get_logger(__name__)
SLA_QUEUE = "agent.owner_question_sla"
Scope = Literal["global", "customer", "temporary"]

# deterministic markers win over the classifier (POC_04 §7 step 5)
_CUSTOMER = re.compile(
    r"\b(isko|usko|inko|unko|ise|use|inhe|unhe|iske liye|uske liye|inke liye|unke liye|for (him|her|them)|"
    r"this customer|is customer|sirf (iske|uske|inke|unke|is customer)|only (for )?(him|her|this customer))\b",
    re.IGNORECASE,
)
_TEMPORARY = re.compile(
    r"\b(aaj|today|tonight|aaj raat|abhi ke liye|for now|is hafte|this week|kal tak|till|until|tak ke liye)\b",
    re.IGNORECASE,
)
STILL_WAITING = {
    "hi-Latn": "Hamari team jaldi aapko batayegi 🙏",
    "hi": "हमारी टीम जल्दी आपको बताएगी 🙏",
    "en": "Our team will get back to you shortly 🙏",
}


class ScopeDecision(BaseModel):
    scope: Scope
    confidence: float = Field(ge=0, le=1)


class QuestionService:
    """QuestionAnswerPort (agent). Also schedules the SLA reminder for new questions."""

    def __init__(self, registry: Registry, model_gateway: ModelGateway) -> None:
        self.registry = registry
        self.gateway = model_gateway

    # ---------------------------------------------------------------- answer (owner → customer)
    async def answer(
        self,
        business_id: UUID,
        question_id: UUID,
        answer: str,
        *,
        member_id: UUID | None,
        source_message_id: UUID | None = None,
        scope: str | None = None,
    ) -> AnswerResult:
        answer = answer.strip()
        async with get_db().tenant_session(business_id) as s:
            q = (
                await s.execute(
                    text("""SELECT q.id, q.short_code, q.question, q.status, q.conversation_id, q.customer_id,
                                   q.source_turn_id, q.answer_scope, q.qa_entry_id, c.display_name
                            FROM owner_questions q JOIN customers c ON c.id = q.customer_id
                            WHERE q.id = :id FOR UPDATE OF q"""),
                    {"id": question_id},
                )
            ).one()
            if str(q.status) in ("answered", "relayed"):  # a duplicate tap / retried webhook
                return AnswerResult(
                    question_code=q.short_code.strip(),
                    scope=str(q.answer_scope or "global"),
                    qa_entry_id=q.qa_entry_id,
                    relayed=str(q.status) == "relayed",
                    already_answered=True,
                )
        decided, uncertain = await self._decide_scope(business_id, q.question, answer, scope)
        valid_until = await self._end_of_today(business_id) if decided == "temporary" else None
        saved_scope: Scope = "customer" if uncertain else decided
        cs = await knowledge.apply_now(
            business_id,
            "learning",
            f"Owner answered: {q.question[:80]}",
            [
                (
                    "qa",
                    "create",
                    None,
                    {
                        "question": q.question,
                        "answer": answer,
                        "scope": saved_scope,
                        "customer_id": str(q.customer_id) if saved_scope == "customer" else None,
                        "valid_until": valid_until.isoformat() if valid_until else None,
                        "source_message_id": str(source_message_id) if source_message_id else None,
                    },
                )
            ],
            member_id=member_id,
        )
        qa_id = cs.items[0].entity_id
        async with get_db().tenant_session(business_id) as s:
            await s.execute(
                text("""UPDATE owner_questions SET status = 'answered', answer = :a, answered_by_member_id = :m,
                               answer_scope = CAST(:sc AS answer_scope), answered_at = now(), qa_entry_id = :qa
                        WHERE id = :id"""),
                {"a": answer, "m": member_id, "sc": saved_scope, "qa": qa_id, "id": question_id},
            )
            relay_msg = await self._relay_message(s, q.conversation_id, q.source_turn_id)
            relayed = False
            if relay_msg is not None:
                await self.registry.require(TurnScheduler).schedule(  # type: ignore[type-abstract]
                    s,
                    business_id=business_id,
                    conversation_id=q.conversation_id,
                    message_id=relay_msg,
                    desk=Desk.CUSTOMER,
                    priority=PriorityClass.P0,
                    ends_thought=True,
                    hints={"relay_question_id": str(question_id)},
                )
                relayed = True
        code = q.short_code.strip()
        await emit(
            "owner_answer",
            PipelineEventStatus.OK,
            business_id=business_id,
            conversation_id=q.conversation_id,
            question=code,
            scope=saved_scope,
            uncertain=uncertain,
        )
        return AnswerResult(
            question_code=code, scope=saved_scope, qa_entry_id=qa_id, relayed=relayed, scope_uncertain=uncertain
        )

    async def set_scope(self, business_id: UUID, question_id: UUID, scope: str) -> AnswerResult:
        """The owner's tap on "for everyone / only this customer" after an uncertain answer."""
        if scope not in ("global", "customer"):
            raise ValueError(f"bad scope {scope}")
        async with get_db().tenant_session(business_id) as s:
            q = (
                await s.execute(
                    text("SELECT short_code, customer_id, qa_entry_id, status FROM owner_questions WHERE id = :id"),
                    {"id": question_id},
                )
            ).one()
        if q.qa_entry_id is None:
            raise LookupError("question not answered yet")
        change = {"scope": scope, "customer_id": str(q.customer_id) if scope == "customer" else None}
        await knowledge.apply_now(business_id, "learning", "Answer scope", [("qa", "update", q.qa_entry_id, change)])
        async with get_db().tenant_session(business_id) as s:
            await s.execute(
                text("UPDATE owner_questions SET answer_scope = CAST(:sc AS answer_scope) WHERE id = :id"),
                {"sc": scope, "id": question_id},
            )
        return AnswerResult(
            question_code=q.short_code.strip(),
            scope=scope,
            qa_entry_id=q.qa_entry_id,
            relayed=str(q.status) == "relayed",
        )

    # ---------------------------------------------------------------- SLA (step 8)
    async def schedule_sla(self, business_id: UUID, question_id: UUID, due_in_s: float) -> None:
        await self.registry.require(JobEnqueuer).enqueue(  # type: ignore[type-abstract]
            SLA_QUEUE,
            {"business_id": str(business_id), "question_id": str(question_id)},
            business_id=business_id,
            priority=PriorityClass.P2,
            dedupe_key=f"oqsla:{question_id}",
            delay_s=max(due_in_s, 0),
        )

    async def run_sla(self, business_id: UUID, question_id: UUID) -> None:
        """Still unanswered at the SLA: tell the customer once that the team will get back; keep the question."""
        async with get_db().tenant_session(business_id) as s:
            q = (
                await s.execute(
                    text("""SELECT q.status, q.conversation_id, c.language FROM owner_questions q
                            JOIN customers c ON c.id = q.customer_id WHERE q.id = :id"""),
                    {"id": question_id},
                )
            ).first()
            if q is None or str(q.status) != "open":
                return
            language = str(q.language or "hi-Latn")
            await gateway.outbound().enqueue(
                s,
                business_id=business_id,
                conversation_id=q.conversation_id,
                blocks=[{"type": "text", "text": STILL_WAITING.get(language, STILL_WAITING["hi-Latn"])}],
                origin="system",
                idempotency_base=f"oqsla:{question_id}",
            )
        await emit("owner_question_sla", PipelineEventStatus.OK, business_id=business_id, question=str(question_id))

    # ---------------------------------------------------------------- helpers
    async def _decide_scope(
        self, business_id: UUID, question: str, answer: str, given: str | None
    ) -> tuple[Scope, bool]:
        """(scope, uncertain). Explicit scope or a deterministic marker → certain; else the classifier role."""
        if given in ("global", "customer", "temporary"):
            return given, False  # type: ignore[return-value]
        if _CUSTOMER.search(answer):
            return "customer", False
        if _TEMPORARY.search(answer):
            return "temporary", False
        prompt = (
            "A shop owner answered a customer's question. Decide who the answer applies to.\n"
            "global = true for every customer (a general fact about the shop or products); "
            "customer = only this one customer; temporary = only for today / a limited time.\n"
            f'Customer question: "{question[:300]}"\nOwner answer: "{answer[:500]}"\n'
            'Reply as JSON: {"scope": "global|customer|temporary", "confidence": 0..1}'
        )
        try:
            decision = await self.gateway.structured(
                "classifier",
                [ChatMessage(role="user", content=prompt)],
                ScopeDecision,
                ctx=CallCtx(business_id=business_id, purpose="answer_scope"),
            )
        except ModelError as exc:
            log.info("answer_scope_classifier_failed", code=exc.code)
            return "global", True
        return decision.scope, decision.confidence < 0.7

    async def _end_of_today(self, business_id: UUID) -> datetime:
        settings = await knowledge.business_settings(business_id)
        local = to_business_tz(utcnow(), str(settings.get("timezone") or "Asia/Kolkata"))
        return datetime.combine(local.date(), time(23, 59, 59), tzinfo=local.tzinfo)

    async def _relay_message(self, s: AsyncSession, conversation_id: UUID, source_turn_id: UUID | None) -> UUID | None:
        """The customer message the relay turn answers: the question's own message, else their latest one."""
        if source_turn_id is not None:
            ids = (
                await s.execute(text("SELECT message_ids FROM conversation_turns WHERE id = :t"), {"t": source_turn_id})
            ).scalar_one_or_none()
            if ids:
                return UUID(str(ids[-1]))
        latest: UUID | None = (
            await s.execute(
                text("""SELECT id FROM messages WHERE conversation_id = :c AND direction = 'inbound'
                        ORDER BY created_at DESC LIMIT 1"""),
                {"c": conversation_id},
            )
        ).scalar_one_or_none()
        return latest
