"""The decision side of proposals (ADR-015, POC_10 §6): the owner said yes or no to an action the agent proposed.

Yes → the stored action runs exactly as proposed (same capability, same arguments; no LLM re-decides). Either way
the outcome is recorded on the request, a relay turn tells the customer in their language, and the owner gets a
short confirmation.

Then the agent offers to learn (POC_10 §6): it proposes the general rule behind the decision — typed conditions
from the playbook's field catalogue, each one checked to hold for the case it came from — and shows it to the
owner word for word: [Always] [Only for <customer>] [Just this once]. A tap makes it a playbook rule (undoable).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

import reflexo.approvals.api as approvals
import reflexo.commerce.api as commerce
import reflexo.knowledge.api as knowledge
from reflexo.agent.capabilities import CAPABILITIES, PROPOSAL_KIND, Capability
from reflexo.agent.questions import relay_message
from reflexo.common.contracts import ModelGatewayPort, OwnerNotifyPort, TurnScheduler
from reflexo.common.db import get_db
from reflexo.common.enums import Desk, PipelineEventStatus, PriorityClass
from reflexo.common.llm import CallCtx, ChatMessage, ModelError
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.xray import emit

log = get_logger(__name__)
Condition = knowledge.playbook.Condition


class Generalisation(BaseModel):
    conditions: list[Condition] = Field(default_factory=list, max_length=3)


class ProposalService:
    """ProposalPort."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    async def decided(self, business_id: UUID, approval_id: UUID, approve: bool, member_id: UUID | None) -> str:
        a = await approvals.get(business_id, approval_id)
        if a is None or a.kind != PROPOSAL_KIND:
            return "I can't find that request any more."
        sub = a.subject
        cap = CAPABILITIES.get(str(sub.get("capability")))
        name = str(sub.get("customer_name") or "the customer")
        summary = str(a.preview.get("summary", ""))
        if cap is None:
            log.warning("proposal_capability_unknown", capability=sub.get("capability"))
            return "That action is no longer available."
        outcome: dict[str, Any]
        if approve:
            try:
                by = str(sub.get("by") or "customer")  # the customer asked; or the owner did it themselves
                result = await cap.execute(business_id, {**sub["args"], "reason": sub.get("reason")}, by)
                outcome = {"approved": True, "done": True, **result}
            except commerce.CommerceError as exc:  # the world changed since the proposal (e.g. now delivered)
                outcome = {"approved": True, "done": False, "reason": str(exc)}
        else:
            outcome = {"approved": False}
        learnable = not sub.get("owner_initiated")  # the owner acting directly is not a decision to generalise
        if learnable and (outcome.get("done") or not approve):
            outcome["learn"] = await self._generalise(business_id, cap, sub, approve)
        await approvals.record_outcome(business_id, approval_id, outcome)
        if sub.get("conversation_id") and (approve or not sub.get("owner_initiated")):
            await self._relay(business_id, a)
        if "learn" in outcome:
            await self._offer_to_learn(business_id, a, cap, outcome["learn"], name)
        await emit(
            "proposal_decided",
            PipelineEventStatus.OK,
            business_id=business_id,
            capability=cap.name,
            approved=approve,
            done=bool(outcome.get("done")),
        )
        if not approve:
            return f"Okay, not doing it. I'll let {name} know."
        if not outcome["done"]:
            return f"Couldn't do it: {outcome['reason']}. I'll let {name} know."
        refund = f" Refund due: {outcome['refund_due']}." if outcome.get("refund_due") else ""
        return f"Done ✅ {cap.label.lower()} {summary}.{refund} I'll tell {name}."

    async def _relay(self, business_id: UUID, a: approvals.Approval) -> None:
        """A turn on the customer's conversation that tells them the outcome (context: `# THE SHOP DECIDED`)."""
        conversation_id = UUID(a.subject["conversation_id"])
        source_turn = a.subject.get("source_turn_id")
        async with get_db().tenant_session(business_id) as s:
            message_id = await relay_message(s, conversation_id, UUID(source_turn) if source_turn else None)
            if message_id is None:
                return
            await self.registry.require(TurnScheduler).schedule(  # type: ignore[type-abstract]
                s,
                business_id=business_id,
                conversation_id=conversation_id,
                message_id=message_id,
                desk=Desk.CUSTOMER,
                priority=PriorityClass.P0,
                ends_thought=True,
                hints={"proposal_id": str(a.id)},
            )

    # ------------------------------------------------------------ learning from the decision

    async def _generalise(
        self, business_id: UUID, cap: Capability, sub: dict[str, Any], approve: bool
    ) -> dict[str, Any]:
        """The rule the owner most likely follows. The model proposes typed conditions; every condition must hold
        for this very case (a rule that would not have covered its own example is wrong) — others are dropped."""
        situation: dict[str, Any] = dict(sub.get("situation") or {})
        catalogue = "\n".join(
            f"- {name} ({f.kind}{': ' + ', '.join(f.values) if f.values else ''}): {f.en}"
            for name, f in knowledge.playbook.FIELDS.items()
        )
        decided = "YES" if approve else "NO"
        prompt = (
            f"A shop's assistant asked the owner whether to do this for a customer: {cap.label.lower()}. "
            f"The owner said {decided}.\nFacts of this case: {json.dumps(situation, default=str)}\n"
            f"Customer's reason: {sub.get('reason', '')!r}\n\n"
            "Propose the general rule the owner most likely follows, so the assistant can decide alone next time. "
            "At most 3 conditions, only from these fields:\n"
            f"{catalogue}\n"
            "Ops: is, is_not, in, not_in, lt, lte, gt, gte, includes. Prefer facts that explain the decision (how "
            "close delivery is, whether it is being prepared, the payment state) over incidental ones (the exact "
            "total, this customer); use round thresholds an owner would say (24, 48 hours). Every condition must be "
            "true for this case. No conditions = always.\n"
            'Reply as JSON: {"conditions": [{"field": "...", "op": "...", "value": ...}]}'
        )
        try:
            proposal = await self.registry.require(ModelGatewayPort).structured(  # type: ignore[type-abstract]
                "builder",
                [ChatMessage(role="user", content=prompt)],
                Generalisation,
                ctx=CallCtx(business_id=business_id, purpose="learn_rule"),
            )
            conditions = [c for c in proposal.conditions if c.holds(situation)]
        except ModelError as exc:
            log.info("learn_generalise_failed", code=exc.code)
            status = situation.get("order.status")  # the one fact every order capability depends on
            conditions = [Condition(field="order.status", op="is", value=str(status))] if status else []
        return {"effect": "allow" if approve else "deny", "conditions": [c.model_dump() for c in conditions]}

    async def _offer_to_learn(
        self, business_id: UUID, a: approvals.Approval, cap: Capability, learn: dict[str, Any], name: str
    ) -> None:
        notifier = self.registry.optional(OwnerNotifyPort)  # type: ignore[type-abstract]
        if notifier is None:
            return
        rule = _draft_rule(cap, learn, "")
        mine = "do this myself" if learn["effect"] == "allow" else "say no myself"
        await notifier.notify(
            business_id,
            "learn",
            f"💡 Next time, should I {mine}?\n“{rule.readback(cap.label)}”",
            dedupe_key=f"learn:{a.id}",
            priority=PriorityClass.P2,
            buttons=[
                (f"lrn:{a.id}:g", "✅ Always"),
                (f"lrn:{a.id}:c", f"Only for {name}"[:20]),
                (f"lrn:{a.id}:o", "Just this once"),
            ],
            ref={"approval_id": str(a.id)},
            about_conversation=UUID(a.subject["conversation_id"]) if a.subject.get("conversation_id") else None,
        )

    async def learn(self, business_id: UUID, approval_id: UUID, scope: str, member_id: UUID | None) -> str:
        a = await approvals.get(business_id, approval_id)
        decision = (await approvals.outcome(business_id, approval_id) or {}).get("outcome") or {}
        if a is None or a.kind != PROPOSAL_KIND or "learn" not in decision:
            return "Nothing to learn from that one."
        if decision.get("learned"):
            return "Already saved ✅"
        if scope == "o":
            await approvals.record_outcome(business_id, approval_id, {**decision, "learned": "once"})
            return "Okay — I'll ask you again next time. 👍"
        cap = CAPABILITIES[str(a.subject["capability"])]
        customer_id = UUID(a.subject["customer_id"]) if scope == "c" else None
        who = f" (only for {a.subject.get('customer_name', 'this customer')})" if customer_id else ""
        says = f"Learned from your decision on {a.preview.get('summary', '')}{who}"
        rule = _draft_rule(cap, decision["learn"], says)
        await knowledge.playbook.add_rule(
            business_id,
            capability=cap.name,
            effect=rule.effect,
            conditions=rule.conditions,
            says=says,
            source="owner_decision",
            customer_id=customer_id,
            source_ref=approval_id,
            summary=f"Playbook: {cap.label}",
            member_id=member_id,
        )
        await approvals.record_outcome(business_id, approval_id, {**decision, "learned": scope})
        return f"Got it ✅ From now on{who}: {rule.readback(cap.label)}. (/undo to remove)"


def _draft_rule(cap: Capability, learn: dict[str, Any], says: str) -> knowledge.playbook.PlaybookRule:
    return knowledge.playbook.PlaybookRule(
        id=UUID(int=0),
        capability=cap.name,
        effect=learn["effect"],
        conditions=[Condition.model_validate(c) for c in learn.get("conditions") or []],
        says=says or "draft",
        source="owner_decision",
    )


def decided_block(a: approvals.Approval, decision: dict[str, Any] | None) -> str:
    """Prompt context for the relay turn: what was asked, what the owner decided, what happened."""
    outcome = (decision or {}).get("outcome") or {}
    if a.subject.get("owner_initiated"):
        why = f" (reason: “{a.subject['reason']}”)" if a.subject.get("reason") else ""
        return (
            "# THE SHOP DECIDED — tell the customer now\n"
            f"The shop itself did this: {str(a.preview.get('summary', '')).lower()}{why}; status now "
            f"{outcome.get('status')}. Tell the customer kindly in their language, briefly (action=send)."
        )
    asked = f"{a.preview.get('label', 'an action').lower()} {a.preview.get('summary', '')}"
    if not outcome.get("approved"):
        result = "The owner decided not to do it. Tell them kindly and offer what is possible instead."
    elif not outcome.get("done"):
        result = f"The owner agreed, but it could not be done: {outcome.get('reason')}. Explain kindly."
    else:
        refund = (
            f" A refund of {outcome['refund_due']} is due; the shop will arrange it."
            if outcome.get("refund_due")
            else ""
        )
        result = f"The owner agreed and it is done (status: {outcome.get('status')}).{refund}"
    return (
        "# THE SHOP DECIDED — tell the customer now\n"
        f"They had asked for: {asked} (their reason: “{a.subject.get('reason', '')}”). {result}\n"
        "Say it in their language, briefly (action=send). Do not ask the shop again."
    )
