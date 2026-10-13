"""Owner desk (POC_04 §3.3, §8, §9; F04/F05/F07): one assistant for the owner on every channel.

prepare (owner timeline across channels, recent files, pending questions/approvals) → model ⇄ tools → reply_owner.
Writes only ever create read-backs + approvals; any approval created in the turn is attached to the reply as
[Confirm] [Cancel] buttons, so confirming never depends on the model remembering to offer it.
"""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.approvals.api as approvals
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner
from reflexo.agent import prompts
from reflexo.agent.checkpointer import get_checkpointer
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.model_gateway.langchain import GatewayChatModel
from reflexo.agent.runs import record_run
from reflexo.agent.tools.base import TOOLS, ToolContext, run_tools
from reflexo.agent.tools.owner import OWNER_TOOLS
from reflexo.common.config import get_settings
from reflexo.common.contracts import TurnHandle, TurnRef
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus, TurnOutcome
from reflexo.common.llm import CallCtx, ModelError, ToolCall, current_call_ctx
from reflexo.common.logging import get_logger
from reflexo.common.money import format_inr
from reflexo.common.timeutil import to_business_tz, utcnow
from reflexo.common.xray import emit

log = get_logger(__name__)
TIMELINE = 30
CHANNEL_TAG = {"whatsapp": "WhatsApp", "telegram": "Telegram", "facebook_messenger": "Messenger"}
FALLBACK_TEXT = "Sorry, I couldn't finish that just now — please try again in a moment."


# ---------------------------------------------------------------- context


@dataclass
class OwnerContext:
    system: str
    dynamic: str
    conversation: gateway.ConversationView
    member_id: UUID
    member_name: str
    timings_ms: dict[str, int] = field(default_factory=dict)


def _fmt(channel: str, m: gateway.MessageView, tz: str) -> str:
    who = {"owner": "owner", "team": "owner", "customer": "owner"}.get(m.author, "you")
    when = to_business_tz(m.created_at, tz).strftime("%d %b %H:%M")
    body = m.text
    for a in m.attachments:
        body += f" [{a.get('kind')}: {a.get('description') or 'not described yet'}]"
    return f"[{CHANNEL_TAG.get(channel, channel)}] {when} {who}: {body}"


async def prepare_owner(ref: TurnRef) -> OwnerContext:
    started = time.perf_counter()
    async with get_db().tenant_session(ref.business_id) as s:
        conv = await gateway.conversation_view(s, ref.conversation_id)
        if conv is None or conv.member_id is None:
            raise LookupError(f"owner conversation {ref.conversation_id} not found")
        inbound = await gateway.messages_by_ids(s, list(dict.fromkeys(ref.message_ids)))
        timeline = await gateway.owner_timeline(s, conv.member_id, limit=TIMELINE, exclude=list(ref.message_ids))
        questions = (
            await s.execute(
                text("""SELECT q.short_code, q.question, c.display_name FROM owner_questions q
                        JOIN customers c ON c.id = q.customer_id WHERE q.status IN ('open', 'asked')
                        ORDER BY q.asked_at DESC LIMIT 5""")
            )
        ).all()
    settings = await knowledge.business_settings(ref.business_id)
    profile = await knowledge.profile_block(ref.business_id)
    files = await owner.search_files(ref.business_id, limit=10)
    waiting = await approvals.pending(ref.business_id, limit=5)
    member_name, member_role = await owner.member_info(ref.business_id, conv.member_id)
    onboarding = await owner.onboarding.get(ref.business_id)
    tz = str(settings.get("timezone") or "Asia/Kolkata")

    cod = settings.get("cod_rule") or {}
    cod_line = (
        "off"
        if not cod.get("enabled")
        else (f"on up to {format_inr(int(cod['max_total_paise']))}" if cod.get("max_total_paise") else "on")
    )
    system = "\n\n".join(
        [
            prompts.render(
                "owner",
                business_name=str(settings.get("name")),
                member_name=member_name,
                member_role=member_role,
                channel=CHANNEL_TAG.get(conv.channel, conv.channel),
            ),
            "# BUSINESS\n" + profile.text,
            f"# SETTINGS\ncash on delivery: {cod_line}",
        ]
    )
    now = to_business_tz(utcnow(), tz)
    dates = "; ".join(
        f"{n} = {(now.date() + timedelta(days=i)).isoformat()}" for i, n in enumerate(["today", "tomorrow"])
    )
    pending = [
        f"question {q.short_code.strip()} from {q.display_name or 'a customer'}: {q.question}" for q in questions
    ]
    pending += [f"waiting for confirmation {a.short_code}: {a.preview.get('summary')}" for a in waiting]
    dynamic = "\n\n".join(
        [
            "# TIMELINE (all your channels, oldest first)\n"
            + ("\n".join(_fmt(ch, m, tz) for ch, m in timeline) or "(new conversation)"),
            "# RECENT FILES\n"
            + ("\n".join(f.line(to_business_tz(f.created_at, tz).strftime("%d %b %H:%M")) for f in files) or "(none)"),
            "# PENDING\n" + ("\n".join(pending) or "(nothing pending)"),
            *([_onboarding_block(onboarding)] if onboarding and onboarding["state"] != "live" else []),
            "# NOW — new message(s) from the owner\n" + "\n".join(_fmt(conv.channel, m, tz) for m in inbound),
            f"Current time: {now.strftime('%a %d %b %Y, %H:%M')} ({tz}); {dates}. Finish with `reply_owner`.",
        ]
    )
    return OwnerContext(
        system=system,
        dynamic=dynamic,
        conversation=conv,
        member_id=conv.member_id,
        member_name=member_name,
        timings_ms={"prepare": int((time.perf_counter() - started) * 1000)},
    )


def _onboarding_block(session: dict[str, Any]) -> str:
    state, data = session["state"], session["data"]
    current = next((g for g in owner.onboarding.GAPS if g.key == data.get("current")), None)
    gap_step = (
        f"Current question key '{current.key}': \"{current.ask}\". When the owner answers, call onboarding_answer "
        "with that key and their words (answer=null if they say later/skip); then ask the next question it returns."
        if current
        else "Ask the remaining onboarding questions via onboarding_answer."
    )
    steps = {
        "sources_requested": "Ask for the menu / price list (photo, PDF, Excel, or typed). When a file appears in "
        "RECENT FILES, call import_from_file with mode='onboarding'; when the owner types their items with prices, "
        "call import_from_text with their full text and mode='onboarding'.",
        "extracting": "The menu is being read, or waits for the owner to confirm the summary (confirm_pending if "
        "they agree in their own words).",
        "gap_questions": gap_step,
        "preview": "The owner is reviewing sample answers. 'theek hai'/ok → onboarding_finish. A correction → "
        "knowledge_change (qa) with the right answer, then ask if everything else is fine.",
    }
    return f"# ONBOARDING (state: {state}) — not live for customers yet\n{steps.get(state, '')}"


# ---------------------------------------------------------------- graph


class OwnerState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    model_calls: int
    tool_calls: int
    nudged: bool
    approvals: list[dict[str, str]]
    reply_text: str | None
    result: Literal["finalize", "fallback"] | None
    fallback_reason: str | None


@dataclass
class OwnerDeps:
    handle: TurnHandle
    prepared: OwnerContext
    tool_ctx: ToolContext
    gateway: ModelGateway


def _deps(config: RunnableConfig) -> OwnerDeps:
    deps = (config.get("configurable") or {}).get("deps")
    if not isinstance(deps, OwnerDeps):
        raise RuntimeError("owner graph invoked without OwnerDeps")
    return deps


def _last_ai(state: OwnerState) -> AIMessage:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            return m
    raise RuntimeError("no model message")


def _tool_dicts() -> list[dict[str, Any]]:
    out = []
    for n in OWNER_TOOLS:
        spec = TOOLS[n].spec()
        out.append(
            {
                "type": "function",
                "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
            }
        )
    return out


async def _prepare(state: OwnerState, config: RunnableConfig) -> dict[str, Any]:
    if state.get("messages"):
        return {}
    p = _deps(config).prepared
    return {
        "messages": [SystemMessage(p.system), HumanMessage(p.dynamic)],
        "model_calls": 0,
        "tool_calls": 0,
        "nudged": False,
        "approvals": [],
        "reply_text": None,
        "result": None,
        "fallback_reason": None,
    }


async def _model(state: OwnerState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    calls = state.get("model_calls", 0)
    if calls >= get_settings().max_model_calls_owner + (1 if state.get("nudged") else 0):  # see the customer graph
        return {"result": "fallback", "fallback_reason": "model_call_limit"}
    await deps.handle.checkpoint("model")
    llm = GatewayChatModel(gateway=deps.gateway, role="owner_desk", purpose="owner_turn")
    choice = "reply_owner" if state.get("nudged") else None  # nudged: must finish; see the customer graph
    try:
        ai = await llm.bind_tools(_tool_dicts(), tool_choice=choice).ainvoke(state["messages"])
    except ModelError as exc:
        return {"result": "fallback", "fallback_reason": f"model:{exc.code}", "model_calls": calls + 1}
    return {"messages": [ai], "model_calls": calls + 1}


def _route_model(state: OwnerState) -> str:
    if state.get("result") == "fallback":
        return END
    names = [c["name"] for c in _last_ai(state).tool_calls]
    if names == ["reply_owner"]:
        return "finish"
    if names:
        return "tools"  # a reply next to other calls is dropped: the model must see the results first
    return "nudge"


async def _finish(state: OwnerState, config: RunnableConfig) -> dict[str, Any]:
    call = _last_ai(state).tool_calls[0]
    reply = str((call.get("args") or {}).get("text") or "").strip()
    if not reply:
        return {"result": "fallback", "fallback_reason": "empty_reply"}
    return {"reply_text": reply, "result": "finalize"}


async def _nudge(state: OwnerState, config: RunnableConfig) -> dict[str, Any]:
    if state.get("nudged"):
        return {"result": "fallback", "fallback_reason": "no_reply_call"}
    return {"messages": [HumanMessage("Finish by calling `reply_owner` with your message.")], "nudged": True}


async def _tools(state: OwnerState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    calls = [
        ToolCall(id=c.get("id") or "", name=c["name"], args=c["args"])
        for c in _last_ai(state).tool_calls
        if c["name"] != "reply_owner"
    ]
    made = state.get("tool_calls", 0) + len(calls)
    if made > get_settings().max_tool_calls_owner:
        return {"result": "fallback", "fallback_reason": "tool_call_limit"}
    if any(TOOLS.get(c.name) and TOOLS[c.name].side_effect for c in calls):
        await deps.handle.checkpoint("tool")
    ctx = dataclasses.replace(deps.tool_ctx, step=state.get("model_calls", 0))
    results = await run_tools(calls, ctx, set(OWNER_TOOLS))
    found = list(state.get("approvals") or [])
    for _, r in results:
        data = (r.get("data") or {}) if isinstance(r, dict) else {}
        approval = data.get("approval")
        if isinstance(approval, dict) and approval.get("id"):
            found.append(
                {
                    "id": str(approval["id"]),
                    "code": str(approval["code"]),
                    "read_back": "\n".join(str(x) for x in data.get("read_back") or []),
                }
            )
    messages = [
        ToolMessage(content=json.dumps(r, ensure_ascii=False, default=str), tool_call_id=c.id, name=c.name)
        for c, r in results
    ]
    return {"messages": messages, "tool_calls": made, "approvals": found}


def _route_step(state: OwnerState) -> str:
    return END if state.get("result") == "fallback" else "model"


def build_owner_graph() -> StateGraph[OwnerState]:
    g: StateGraph[OwnerState] = StateGraph(OwnerState)
    g.add_node("prepare", _prepare)
    g.add_node("model", _model)
    g.add_node("tools", _tools)
    g.add_node("nudge", _nudge)
    g.add_node("finish", _finish)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "model")
    g.add_conditional_edges("model", _route_model, ["finish", "tools", "nudge", END])
    g.add_conditional_edges("tools", _route_step, ["model", END])
    g.add_conditional_edges("nudge", _route_step, ["model", END])
    g.add_edge("finish", END)
    return g


# ---------------------------------------------------------------- runner


def with_read_back(text_: str, found: list[dict[str, str]]) -> str:
    """The owner must see exactly what they confirm: any read-back line the model left out is added verbatim."""
    missing = [
        line
        for a in found
        for line in (a.get("read_back") or "").splitlines()
        if line.strip() and line.strip() not in text_
    ]
    return text_ if not missing else "\n".join(missing) + "\n\n" + text_


def reply_blocks(text_: str, found: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The reply, with [Confirm] [Cancel] for the approval made this turn (several: one Confirm per code)."""
    text_ = with_read_back(text_, found)
    if not found:
        return [{"type": "text", "text": text_}]
    if len(found) == 1:
        a = found[0]
        options = [{"id": f"apv:{a['id']}:a", "title": "✅ Confirm"}, {"id": f"apv:{a['id']}:r", "title": "✖ Cancel"}]
    else:
        options = [{"id": f"apv:{a['id']}:a", "title": f"✅ Confirm {a['code']}"} for a in found[-3:]]
    if len(text_) <= 1024:
        return [{"type": "buttons", "text": text_, "options": options}]
    return [{"type": "text", "text": text_}, {"type": "buttons", "text": "Confirm?", "options": options}]


class OwnerDesk:
    def __init__(self, model_gateway: ModelGateway) -> None:
        self.gateway = model_gateway
        self._graph: CompiledStateGraph[OwnerState] | None = None

    async def graph(self) -> CompiledStateGraph[OwnerState]:
        if self._graph is None:
            self._graph = build_owner_graph().compile(checkpointer=await get_checkpointer())
        return self._graph

    async def run_turn(self, handle: TurnHandle) -> None:
        ref = handle.ref
        started = time.perf_counter()
        ctx = CallCtx(
            business_id=ref.business_id, purpose="owner_turn", turn_id=ref.turn_id, conversation_id=ref.conversation_id
        )
        token = current_call_ctx.set(ctx)
        try:
            prepared = await prepare_owner(ref)
            await emit(
                "context",
                PipelineEventStatus.OK,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                turn_id=ref.turn_id,
                duration_ms=prepared.timings_ms["prepare"],
                desk="owner",
            )
            deps = OwnerDeps(
                handle=handle,
                prepared=prepared,
                tool_ctx=ToolContext(
                    business_id=ref.business_id,
                    desk="owner",
                    channel=prepared.conversation.channel,
                    conversation_id=ref.conversation_id,
                    customer_id=None,
                    member_id=prepared.member_id,
                    turn_id=ref.turn_id,
                ),
                gateway=self.gateway,
            )
            graph = await self.graph()
            config: RunnableConfig = {"configurable": {"thread_id": f"owner:{ref.turn_id}", "deps": deps}}
            graph_started = time.perf_counter()
            final = await graph.ainvoke({}, config)
            graph_ms = int((time.perf_counter() - graph_started) * 1000)
        finally:
            current_call_ctx.reset(token)

        ok = final.get("result") == "finalize" and final.get("reply_text")
        reply = str(final["reply_text"]) if ok else FALLBACK_TEXT
        found: list[dict[str, str]] = list(final.get("approvals") or [])
        blocks = reply_blocks(reply, found if ok else [])
        outcome = TurnOutcome.REPLIED if ok else TurnOutcome.FALLBACK

        async def write(s: AsyncSession) -> None:
            await gateway.outbound().enqueue(
                s,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                blocks=blocks,
                origin="ai_turn",
                idempotency_base=f"turn:{ref.turn_id}",
                turn_id=ref.turn_id,
            )
            await record_run(
                s,
                ctx,
                desk="owner",
                prompt_version=prompts.prompt_version("owner"),
                model_primary=self.gateway.config.role("owner_desk").primary,
                latency_ms=int((time.perf_counter() - started) * 1000),
                latency_breakdown={
                    "prepare": prepared.timings_ms["prepare"],
                    "graph": graph_ms,
                    "fallback_reason": final.get("fallback_reason"),
                    "approvals": [a["code"] for a in found],
                },
                outcome=str(outcome),
                topic=None,
                tier="t0",
                grounded=False,
                gate_result=None,
                error={"reason": final.get("fallback_reason")} if not ok else None,
            )

        await handle.commit_reply(write, outcome)
        await emit(
            "reply",
            PipelineEventStatus.OK,
            business_id=ref.business_id,
            conversation_id=ref.conversation_id,
            turn_id=ref.turn_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            action="send" if ok else "fallback",
            desk="owner",
            model_calls=ctx.stats.model_calls,
            approvals=len(found),
        )

    async def run_fallback(self, ref: TurnRef) -> None:
        async with get_db().tenant_session(ref.business_id) as s:
            await gateway.outbound().enqueue(
                s,
                business_id=ref.business_id,
                conversation_id=ref.conversation_id,
                blocks=[{"type": "text", "text": FALLBACK_TEXT}],
                origin="system",
                turn_id=ref.turn_id,
                idempotency_base=f"fallback:{ref.turn_id}",
            )
