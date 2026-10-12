"""The decision side of proposals (ADR-015, POC_10 §6): the owner said yes or no to an action the agent proposed.

Yes → the stored action runs exactly as proposed (same capability, same arguments; no LLM re-decides). Either way
the outcome is recorded on the request, a relay turn tells the customer in their language, and the owner gets a
short confirmation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import reflexo.approvals.api as approvals
import reflexo.commerce.api as commerce
from reflexo.agent.capabilities import CAPABILITIES, PROPOSAL_KIND
from reflexo.agent.questions import relay_message
from reflexo.common.contracts import TurnScheduler
from reflexo.common.db import get_db
from reflexo.common.enums import Desk, PipelineEventStatus, PriorityClass
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.xray import emit

log = get_logger(__name__)


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
                result = await cap.execute(business_id, {**sub["args"], "reason": sub.get("reason")}, "customer")
                outcome = {"approved": True, "done": True, **result}
            except commerce.CommerceError as exc:  # the world changed since the proposal (e.g. now delivered)
                outcome = {"approved": True, "done": False, "reason": str(exc)}
        else:
            outcome = {"approved": False}
        await approvals.record_outcome(business_id, approval_id, outcome)
        await self._relay(business_id, a)
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


def decided_block(a: approvals.Approval, decision: dict[str, Any] | None) -> str:
    """Prompt context for the relay turn: what was asked, what the owner decided, what happened."""
    outcome = (decision or {}).get("outcome") or {}
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
