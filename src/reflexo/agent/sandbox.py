"""Customer-desk sandbox (POC_05 §3.4 `preview`): the real graph, tools and output gate on an in-memory customer —
no conversation rows, no sends, no side effects (only read tools are bound). Used to show the owner how the
assistant will answer before going live.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.agent.context import build_context
from reflexo.agent.graphs.customer import TurnDeps, build_graph
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.schemas import AgentOutput
from reflexo.agent.tools.base import ToolContext
from reflexo.common.ids import new_id
from reflexo.common.llm import CallCtx, current_call_ctx
from reflexo.common.timeutil import utcnow

READ_TOOLS = ["catalog_search", "catalog_get", "knowledge_lookup", "delivery_quote"]
ASK_OWNER = "(I'd check this with you first)"


class _NoCheckpoints:
    """Stands in for TurnHandle: a sandbox run has nothing to resume."""

    async def checkpoint(self, step: str) -> None:
        return None


async def preview_answer(model_gateway: ModelGateway, business_id: UUID, question: str) -> str:
    conv = gateway.ConversationView(
        id=new_id(),
        business_id=business_id,
        channel="whatsapp",
        provider="meta_whatsapp",
        connection_id=new_id(),
        participant_role="customer",
        customer=gateway.CustomerView(
            id=new_id(), display_name=None, phone_e164=None, language=None, facts=[], summary=None
        ),
        member_id=None,
        summary=None,
        job_description=None,
        simulated=True,
        capabilities=gateway.capability_summary("meta_whatsapp"),
    )
    message = gateway.MessageView(
        id=new_id(), direction="inbound", author="customer", text=question, created_at=utcnow()
    )
    prepared = await build_context(business_id, conv, [message], [])
    deps = TurnDeps(
        handle=_NoCheckpoints(),  # type: ignore[arg-type]
        prepared=prepared,
        tool_ctx=ToolContext(
            business_id=business_id,
            desk="customer",
            channel="whatsapp",
            conversation_id=None,
            customer_id=None,
            member_id=None,
            turn_id=new_id(),
        ),
        gateway=model_gateway,
        bound=READ_TOOLS,
    )
    token = current_call_ctx.set(CallCtx(business_id=business_id, purpose="onboarding_preview"))
    try:
        final = await build_graph().compile().ainvoke({}, {"configurable": {"thread_id": str(new_id()), "deps": deps}})
    finally:
        current_call_ctx.reset(token)
    if final.get("result") == "fallback" or not final.get("output"):
        return ASK_OWNER
    output = AgentOutput.model_validate(final["output"])
    gate = final.get("gate") or {}
    if output.action != "send" or gate.get("rewritten_action"):
        return ASK_OWNER
    return await render_text(business_id, output)


async def render_text(business_id: UUID, output: AgentOutput) -> str:
    """Blocks as the owner would picture them on WhatsApp (cards as one line)."""
    parts: list[str] = []
    for block in output.blocks:
        data: dict[str, Any] = block.model_dump()
        kind = data.get("type")
        if kind == "text":
            parts.append(str(data["text"]))
        elif kind == "product":
            card = await knowledge.get_card(business_id, UUID(str(data["offering_id"])))
            if card:
                prices = ", ".join(f"{v.name} {v.price_display}" for v in card.variants if v.price_display)
                parts.append(f"[card: {card.name}: {prices}]")
        elif kind == "carousel":
            cards = await knowledge.cards_for(business_id, [UUID(str(i["offering_id"])) for i in data["items"]], [])
            parts.append("[cards: " + ", ".join(c.name for c in cards) + "]")
        elif kind in ("buttons", "list"):
            options = " / ".join(str(o.get("title")) for o in data.get("options", []))
            parts.append(f"{data.get('text', '')} [{options}]")
    return "\n".join(p for p in parts if p).strip() or ASK_OWNER
