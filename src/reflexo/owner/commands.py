"""Deterministic owner commands (POC_04 §9; POC_03 §3.2 step 12): handled without the model.

* `1234 <answer>`, a tap on a question button (`oq:<id>:<n>`), or a swipe-reply to the question message → the
  ask-the-owner answer (agent's QuestionAnswerPort);
* `oqs:<id>:g|c` (everyone / this customer) → the scope of an answer that was saved for one customer only;
* `/undo` → revert the last applied knowledge change set; `/pause` / `/resume` → AI off / on for customers;
  `/help`.
Everything else returns False and becomes an owner-desk turn.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.approvals.api as approvals
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.common.contracts import AnswerResult, ProposalPort, QuestionAnswerPort, WorkflowPort
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.xray import emit
from reflexo.owner.notify import notification_for_reply

log = get_logger(__name__)
_ANSWER = re.compile(r"^\s*(\d{4})\s*[:.\-–]?\s+(\S.*)$", re.DOTALL)
_OPTION = re.compile(r"^(oq|oqs):([0-9a-f-]{36}):(.+)$")
_APPROVAL_BUTTON = re.compile(r"^apv:([0-9a-f-]{36}):([ar])$")
_YES = r"(?:yes|y|haan|han|ha|ok|okay|confirm|done|theek hai|kar do)"
_NO = r"(?:no|n|nahi|nahin|cancel|mat karo|rehne do)"
_DECIDE_CODE = re.compile(rf"^\s*({_YES}|{_NO})\s*[,:]?\s*(\d{{4}})\s*[.!]*\s*$", re.IGNORECASE)
_DECIDE_BARE = re.compile(rf"^\s*({_YES}|{_NO})\s*[.!👍✅]*\s*$", re.IGNORECASE)
I_WILL_TELL = "later"  # option id of "Main batati hoon": not an answer
HELP = (
    "Commands:\n"
    "• `1234 <answer>` — answer customer question 1234 (or reply to the question message)\n"
    "• `/undo` — undo the last change\n"
    "• `/pause` / `/resume` — stop / restart AI replies to customers\n"
    "Anything else: just tell me what you need."
)


class OwnerCommands:
    """OwnerCommandPort."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    async def try_handle(
        self,
        business_id: UUID,
        member_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        text_: str | None,
        option_id: str | None,
        reply_to_external_id: str | None = None,
    ) -> bool:
        body = (text_ or "").strip()
        handled = await self._dispatch(
            business_id, member_id, conversation_id, message_id, body, option_id, reply_to_external_id
        )
        if handled:
            await emit("owner_command", PipelineEventStatus.OK, business_id=business_id, command=handled)
        return handled is not None

    async def _dispatch(
        self,
        business_id: UUID,
        member_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        body: str,
        option_id: str | None,
        reply_to: str | None,
    ) -> str | None:
        say = _Replier(business_id, conversation_id, message_id)
        if option_id and (m := _OPTION.match(option_id)):
            kind, qid, value = m.group(1), UUID(m.group(2)), m.group(3)
            if kind == "oqs":  # ids stay ≤ 48 chars: g = everyone, c = this customer
                scope = {"g": "global", "c": "customer"}.get(value)
                if scope is None:
                    return None
                result = await self._answers().set_scope(business_id, qid, scope)
                await say("✅ Saved for everyone." if scope == "global" else "✅ Kept for this customer only.")
                log.info("owner_scope_set", question=result.question_code, scope=scope)
                return "scope"
            question = await _question(business_id, question_id=qid)
            if question is None:
                await say("That question is no longer open.")
                return "answer"
            if value == I_WILL_TELL:
                await say(f"Ok 👍 Reply `{question['code']} <answer>` when you're ready.")
                return "answer_later"
            options = question["options"] or []
            idx = int(value) if value.isdigit() else -1
            if not 0 <= idx < len(options):
                return None
            # A typed message the gateway matched to a button ("5309 Haan, sab cakes … ₹50 extra" → [Haan]) keeps
            # the owner's full words; only a bare tap or the option word itself is the option.
            typed = (m2.group(2) if (m2 := _ANSWER.match(body)) else body).strip()
            chosen = str(options[idx])
            answer = typed if typed and typed.lower().strip(" .!") != chosen.lower() else chosen
            await self._answer(say, business_id, member_id, message_id, question, answer)
            return "answer"

        if option_id and option_id.startswith("apv:") and len(body.split()) > 4:
            option_id = None  # a typed sentence the gateway matched to [Confirm] is an instruction, not a tap
        if option_id and (m := _APPROVAL_BUTTON.match(option_id)):
            await self._decide(say, business_id, member_id, m.group(2) == "a", approval_id=UUID(m.group(1)))
            return "approval"
        if (m := _DECIDE_CODE.match(body)) is not None:
            approve = re.fullmatch(_YES, m.group(1), re.IGNORECASE) is not None
            if await self._decide(say, business_id, member_id, approve, code=m.group(2)):
                return "approval"
        elif (m := _DECIDE_BARE.match(body)) is not None:
            only = await _single_recent_approval(business_id, conversation_id)
            if only is not None:
                approve = re.fullmatch(_YES, m.group(1), re.IGNORECASE) is not None
                await self._decide(say, business_id, member_id, approve, approval_id=only)
                return "approval"

        if body.lower() in ("/help", "help"):
            await say(HELP)
            return "help"
        if body.lower() in ("/undo", "undo"):
            await self._undo(say, business_id, member_id)
            return "undo"
        if body.lower() in ("/pause", "/resume"):
            mode = "off" if body.lower() == "/pause" else "tiered"
            await gateway.set_customer_mode(business_id, mode)
            await say(
                "⏸️ AI replies to customers are paused. Send /resume to restart."
                if mode == "off"
                else "▶️ AI replies to customers are back on."
            )
            return body.lower().lstrip("/")

        if (m := _ANSWER.match(body)) is not None:
            question = await _question(business_id, code=m.group(1))
            if question is not None:
                if question["status"] in ("answered", "relayed"):
                    await say(f"Question {question['code']} was already answered ✅")
                else:
                    await self._answer(say, business_id, member_id, message_id, question, m.group(2).strip())
                return "answer"
        if reply_to and body:
            note = await notification_for_reply(business_id, conversation_id, reply_to)
            replied_qid = (note or {}).get("question_id")
            if isinstance(replied_qid, str):
                question = await _question(business_id, question_id=UUID(replied_qid))
                if question is not None and question["status"] not in ("answered", "relayed"):
                    await self._answer(say, business_id, member_id, message_id, question, body)
                    return "answer"
        return None

    async def _answer(
        self, say: _Replier, business_id: UUID, member_id: UUID, message_id: UUID, q: dict[str, Any], answer: str
    ) -> None:
        result: AnswerResult = await self._answers().answer(
            business_id, q["id"], answer, member_id=member_id, source_message_id=message_id
        )
        who = q["customer_name"] or "the customer"
        if result.already_answered:
            await say(f"Question {result.question_code} was already answered ✅")
            return
        sent = f"Sending your answer to {who}" if result.relayed else "Saved"
        if result.scope_uncertain:  # kept private until the owner says it applies to everyone
            await say(
                f"✅ {sent}. Saved for {who} only for now — should it apply to everyone?",
                buttons=[(f"oqs:{q['id']}:g", "Everyone"), (f"oqs:{q['id']}:c", f"Only {who}"[:20])],
            )
            return
        where = {"global": "for everyone", "customer": f"for {who} only", "temporary": "for today"}[result.scope]
        await say(f"✅ {sent}. Saved {where} — next time I'll answer this myself. (/undo to remove)")

    async def _decide(
        self,
        say: _Replier,
        business_id: UUID,
        member_id: UUID,
        approve: bool,
        *,
        approval_id: UUID | None = None,
        code: str | None = None,
    ) -> bool:
        """Record and carry out a decision; False if there is no such request (the text was something else)."""
        outcome = await execute_decision(
            self.registry, business_id, member_id, approve, approval_id=approval_id, code=code
        )
        if outcome is None:
            return False
        if outcome:  # "" = a workflow owns the follow-up message
            await say(outcome)
        return True

    async def _undo(self, say: _Replier, business_id: UUID, member_id: UUID) -> None:
        try:
            undone = await knowledge.revert(business_id, member_id=member_id)
        except LookupError:
            await say("Nothing to undo.")
            return
        except knowledge.ChangeConflict:
            await say("Can't undo: those items changed after that edit. Tell me what to fix instead.")
            return
        await say(f"↩️ {undone.summary} ✅")

    def _answers(self) -> QuestionAnswerPort:
        return self.registry.require(QuestionAnswerPort)  # type: ignore[type-abstract]


class _Replier:
    """Short deterministic replies on the owner's conversation (idempotent per inbound message)."""

    def __init__(self, business_id: UUID, conversation_id: UUID, message_id: UUID) -> None:
        self.business_id, self.conversation_id, self.message_id = business_id, conversation_id, message_id
        self.n = 0

    async def __call__(self, text_: str, buttons: list[tuple[str, str]] | None = None) -> None:
        block: dict[str, Any] = (
            {"type": "buttons", "text": text_, "options": [{"id": i, "title": t} for i, t in buttons]}
            if buttons
            else {"type": "text", "text": text_}
        )
        async with get_db().tenant_session(self.business_id) as s:
            await gateway.outbound().enqueue(
                s,
                business_id=self.business_id,
                conversation_id=self.conversation_id,
                blocks=[block],
                origin="system",
                idempotency_base=f"cmd:{self.message_id}:{self.n}",
            )
        self.n += 1


async def execute_decision(
    registry: Registry,
    business_id: UUID,
    member_id: UUID | None,
    approve: bool,
    *,
    approval_id: UUID | None = None,
    code: str | None = None,
) -> str | None:
    """Decide an approval and carry it out. Returns the owner-facing result, "" when a waiting workflow (import,
    automation) takes over and reports itself, or None if there is no such request."""
    decision = await approvals.decide(
        business_id, approve=approve, member_id=member_id, via=None, approval_id=approval_id, code=code
    )
    if decision is None:
        return None
    a = decision.approval
    if decision.already_decided:
        return "Already done ✅" if a.status == "approved" else f"That was already {a.status}."
    if decision.expired:
        return "That confirmation expired — tell me again and I'll redo the read-back."
    subject = a.subject
    if subject.get("workflow_id"):  # the workflow waits for this decision and applies/reports itself
        delivered = await registry.require(WorkflowPort).signal(  # type: ignore[type-abstract]
            str(subject["workflow_id"]), "approval_decided", {"approve": approve, "approval_id": str(a.id)}
        )
        return "" if delivered else "That review has already ended — send it again if you still want it."
    if a.kind == "tool_call":  # an action the agent proposed (ADR-015): the agent carries out the decision
        return await registry.require(ProposalPort).decided(  # type: ignore[type-abstract]
            business_id, a.id, approve, member_id
        )
    if not approve:
        return "Cancelled — nothing changed."
    try:
        if a.kind == "change_confirmation" and subject.get("change_set_id"):
            await knowledge.apply(business_id, UUID(subject["change_set_id"]))
            return f"Done ✅ {a.preview.get('summary', '')} updated. (/undo to revert)"
        if a.kind == "change_confirmation" and subject.get("settings_patch"):
            await knowledge.patch_settings(business_id, subject["settings_patch"])
            return f"Done ✅ {a.preview.get('summary', 'Settings')} updated."
        if a.kind == "outbound_draft":
            async with get_db().tenant_session(business_id) as s:
                await gateway.outbound().enqueue(
                    s,
                    business_id=business_id,
                    conversation_id=UUID(subject["conversation_id"]),
                    blocks=[{"type": "text", "text": subject["text"]}],
                    origin="owner_command",
                    idempotency_base=f"approved:{a.id}",
                )
            return f"Sent ✅ to {subject.get('to', 'the customer')}."
    except knowledge.ChangeConflict:
        return "Not applied: those items changed after the read-back. Tell me again and I'll redo it."
    log.warning("approval_kind_unhandled", kind=a.kind)
    return "Approved ✅"


async def _single_recent_approval(business_id: UUID, conversation_id: UUID) -> UUID | None:
    """A bare "yes"/"no" decides the one confirmation this chat is waiting on (asked in the last 30 minutes)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id FROM approval_requests WHERE status = 'pending' AND expires_at > now()
                          AND created_at > now() - interval '30 minutes'
                          AND subject->>'owner_conversation_id' = :c ORDER BY created_at DESC LIMIT 2"""),
                {"c": str(conversation_id)},
            )
        ).all()
    return rows[0].id if len(rows) == 1 else None


async def _question(
    business_id: UUID, *, code: str | None = None, question_id: UUID | None = None
) -> dict[str, Any] | None:
    """An open (or just answered) question by short code or id; the newest one if a code was reused."""
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("""SELECT q.id, q.short_code, q.status, q.options, c.display_name FROM owner_questions q
                        JOIN customers c ON c.id = q.customer_id
                        WHERE (CAST(:id AS uuid) IS NOT NULL AND q.id = CAST(:id AS uuid))
                           OR (CAST(:code AS text) IS NOT NULL AND q.short_code = :code
                               AND q.status IN ('open', 'asked', 'answered', 'relayed'))
                        ORDER BY (q.status IN ('open', 'asked')) DESC, q.created_at DESC LIMIT 1"""),
                {"id": question_id, "code": code},
            )
        ).first()
    if row is None or str(row.status) in ("expired", "cancelled"):
        return None
    return {
        "id": row.id,
        "code": row.short_code.strip(),
        "status": str(row.status),
        "options": row.options,
        "customer_name": row.display_name,
    }
