"""Onboarding activities and `OnboardingPreviewWorkflow` (POC_05 §3.4).

After an onboarding import: ask the first missing essential, or go straight to the preview. The preview generates
5 realistic customer questions from the catalogue and profile and answers each with the customer desk sandbox (real
graph and gate, no sends), then shows the owner "this is how I'll answer".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    import reflexo.agent.api as agent
    import reflexo.gateway.api as gateway
    import reflexo.knowledge.api as knowledge
    import reflexo.owner.api as owner
    from reflexo.common.db import get_db
    from reflexo.common.enums import PipelineEventStatus
    from reflexo.common.language import fixed
    from reflexo.common.llm import CallCtx, ChatMessage
    from reflexo.common.xray import emit

PREVIEW_QUESTIONS = 5


class PreviewQuestions(BaseModel):
    questions: list[str] = Field(min_length=1, max_length=8)


async def _say(business_id: UUID, conversation_id: UUID, key: str, text_: str) -> None:
    async with get_db().tenant_session(business_id) as s:
        await gateway.outbound().enqueue(
            s,
            business_id=business_id,
            conversation_id=conversation_id,
            blocks=[{"type": "text", "text": text_}],
            origin="system",
            idempotency_base=key,
        )


@activity.defn(name="onboarding.after_import")
async def after_import(inp: dict[str, Any]) -> dict[str, Any]:
    """Ask the first missing essential; {"preview": True} when nothing is missing."""
    business_id, conversation_id = UUID(inp["business_id"]), UUID(inp["reply_conversation_id"])
    gap = await owner.onboarding.next_gap(business_id)
    if gap is None:
        return {"preview": True}
    await owner.onboarding.set_state(business_id, "gap_questions", {"current": gap.key})
    await _say(
        business_id,
        conversation_id,
        f"onb:{inp['import_run_id']}:gap",
        f"{gap.ask}\n" + fixed("(or say 'later')", "(ya 'baad mein' likhiye)"),
    )
    return {"preview": False}


@activity.defn(name="onboarding.preview")
async def preview(inp: dict[str, Any]) -> list[dict[str, str]]:
    business_id, conversation_id = UUID(inp["business_id"]), UUID(inp["reply_conversation_id"])
    await owner.onboarding.set_state(business_id, "preview")
    cards = await knowledge.search_catalog(business_id, "cake", k=8)
    profile = await knowledge.profile_block(business_id)
    prompt = (
        f"Write {PREVIEW_QUESTIONS} short, realistic messages customers would send this shop on WhatsApp — "
        + fixed("in English", "a mix of Hinglish and English")
        + ": prices of specific items, eggless/custom options, delivery, payment, timings.\n"
        f"Items: {', '.join(c.name for c in cards) or 'see profile'}\nProfile:\n{profile.text[:1500]}"
    )
    gw = agent.gateway()
    generated = await gw.structured(
        "extract",
        [ChatMessage(role="user", content=prompt)],
        PreviewQuestions,
        ctx=CallCtx(business_id=business_id, purpose="onboarding.preview_questions"),
    )
    pairs = []
    for q in generated.questions[:PREVIEW_QUESTIONS]:
        pairs.append({"q": q, "a": await agent.preview_answer(business_id, q)})
    await owner.onboarding.set_state(business_id, "preview", {"preview_qas": pairs})
    body = fixed("Here's how I'd answer 👇", "Aise jawab dungi 👇") + "\n\n"
    body += "\n\n".join(f"❓ {p['q']}\n💬 {p['a']}" for p in pairs)
    body += fixed(
        "\n\nTell me if anything is wrong — otherwise reply 'ok' and I'll go live.",
        "\n\nKuch galat ho to batayiye — warna 'theek hai' likhiye aur main live ho jaungi.",
    )
    await _say(business_id, conversation_id, f"onb:{inp.get('import_run_id') or inp['preview_id']}:preview", body)
    await emit("onboarding", PipelineEventStatus.OK, business_id=business_id, step="preview", questions=len(pairs))
    return pairs


def preview_failed_text() -> str:
    return fixed(
        "I couldn't prepare the preview just now 🙏 — reply 'preview' to try again, or 'go live' to go live now.",
        "Preview abhi nahi ban paya 🙏 — 'preview' likhiye to dobara try karti hoon, "
        "ya 'live karo' to seedha live ho jaungi.",
    )


@activity.defn(name="onboarding.preview_failed")
async def preview_failed(inp: dict[str, Any]) -> None:
    """The preview could not be generated (model down): tell the owner how to continue instead of stalling."""
    business_id = UUID(inp["business_id"])
    key = f"onb:{inp.get('import_run_id') or inp['preview_id']}:preview_failed"
    await _say(business_id, UUID(inp["reply_conversation_id"]), key, preview_failed_text())
    await emit("onboarding", PipelineEventStatus.ERROR, business_id=business_id, step="preview")


ONBOARDING_ACTIVITIES: list[Callable[..., Any]] = [after_import, preview, preview_failed]
PREVIEW_TIMEOUT = timedelta(minutes=3)


@workflow.defn(name="OnboardingPreviewWorkflow")
class OnboardingPreviewWorkflow:
    """Started by the owner desk once the gap questions are done (id `onboarding-preview-<business>-<n>`)."""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await run_preview(inp)


async def run_preview(inp: dict[str, Any]) -> dict[str, Any]:
    """Workflow code (import continuation and the preview workflow): preview, or tell the owner it failed."""
    try:
        pairs = await workflow.execute_activity(
            preview, inp, start_to_close_timeout=PREVIEW_TIMEOUT, retry_policy=RetryPolicy(maximum_attempts=2)
        )
    except ActivityError:
        await workflow.execute_activity(
            preview_failed,
            inp,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return {"questions": 0}
    return {"questions": len(pairs)}
