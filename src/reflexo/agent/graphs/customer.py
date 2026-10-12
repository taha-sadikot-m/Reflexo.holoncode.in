"""Customer desk graph (POC_04 §4): prepare → model ⇄ tools → gate → (finalize | fallback).

The graph only decides; side effects of the turn (sending, owner questions, agent_runs) happen in the desk
runner after the graph ends, inside `TurnHandle.commit_reply`. Tools with side effects use the ledger.
Per-turn dependencies travel in `config["configurable"]["deps"]` (never checkpointed).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from reflexo.agent import grounding, output_gate
from reflexo.agent.context import PreparedContext, without_customer_words
from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.agent.model_gateway.langchain import GatewayChatModel
from reflexo.agent.schemas import AgentOutput, GateResult
from reflexo.agent.tools.base import TOOLS, ToolContext, run_tools
from reflexo.common.config import get_settings
from reflexo.common.contracts import TurnHandle
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.llm import ModelError, ToolCall
from reflexo.common.xray import emit


class CustomerState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    model_calls: int
    tool_calls: int
    nudged: bool
    repair_attempted: bool
    output: dict[str, Any] | None
    gate: dict[str, Any] | None
    result: Literal["finalize", "fallback"] | None
    fallback_reason: str | None


@dataclass
class TurnDeps:
    handle: TurnHandle
    prepared: PreparedContext
    tool_ctx: ToolContext
    gateway: ModelGateway
    bound: list[str]
    wa_display_number: str | None = None


def _deps(config: RunnableConfig) -> TurnDeps:
    deps = (config.get("configurable") or {}).get("deps")
    if not isinstance(deps, TurnDeps):
        raise RuntimeError("customer graph invoked without TurnDeps")
    return deps


def _tool_dicts(names: list[str]) -> list[dict[str, Any]]:
    out = []
    for n in names:
        spec = TOOLS[n].spec()
        out.append(
            {
                "type": "function",
                "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
            }
        )
    return out


def _calls(msg: AIMessage) -> list[ToolCall]:
    return [ToolCall(id=c.get("id") or "", name=c["name"], args=c["args"]) for c in msg.tool_calls]


def _last_ai(state: CustomerState) -> AIMessage:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            return m
    raise RuntimeError("no model message")


# ---------------------------------------------------------------- nodes


async def prepare_node(state: CustomerState, config: RunnableConfig) -> dict[str, Any]:
    if state.get("messages"):
        return {}  # resumed from a checkpoint: the prompt is already in the state
    p = _deps(config).prepared
    return {
        "messages": [SystemMessage(p.system), HumanMessage(p.dynamic)],
        "model_calls": 0,
        "tool_calls": 0,
        "nudged": False,
        "repair_attempted": False,
        "output": None,
        "gate": None,
        "result": None,
        "fallback_reason": None,
    }


async def model_node(state: CustomerState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    calls = state.get("model_calls", 0)
    # The nudged call only moves an answer the model already wrote into `reply`; refusing it at the limit would
    # throw that answer away for a holding message (live: guessed id → search → order_start → prose → refused).
    if calls >= get_settings().max_model_calls_customer + (1 if state.get("nudged") else 0):
        return {"result": "fallback", "fallback_reason": "model_call_limit"}
    await deps.handle.checkpoint("model")
    llm = GatewayChatModel(gateway=deps.gateway, role="customer_desk", purpose="customer_turn")
    # Auto by default: forcing a tool call on every step (mode ANY) fixed prose answers but pushed Hinglish replies
    # into Devanagari in live tests. A prose answer is nudged, and the nudged step may only call `reply` — the
    # model then transcribes its own draft (8/8 live, in the right script).
    choice = "reply" if state.get("nudged") else None
    try:
        ai = await llm.bind_tools(_tool_dicts(deps.bound), tool_choice=choice).ainvoke(state["messages"])
    except ModelError as exc:
        return {"result": "fallback", "fallback_reason": f"model:{exc.code}", "model_calls": calls + 1}
    return {"messages": [ai], "model_calls": calls + 1}


def route_after_model(state: CustomerState) -> str:
    if state.get("result") == "fallback":
        return END
    ai = _last_ai(state)
    names = [c["name"] for c in ai.tool_calls]
    if "reply" in names:
        return "gate"
    if names:
        return "tools"
    return "nudge"


async def nudge_node(state: CustomerState, config: RunnableConfig) -> dict[str, Any]:
    if state.get("nudged"):
        return {"result": "fallback", "fallback_reason": "no_reply_call"}
    return {
        "messages": [HumanMessage("You must finish by calling the `reply` tool with your answer as blocks.")],
        "nudged": True,
    }


async def tools_node(state: CustomerState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    calls = [c for c in _calls(_last_ai(state)) if c.name != "reply"]
    made = state.get("tool_calls", 0) + len(calls)
    if made > get_settings().max_tool_calls_customer:
        return {"result": "fallback", "fallback_reason": "tool_call_limit"}
    if any(TOOLS.get(c.name) and TOOLS[c.name].side_effect for c in calls):
        await deps.handle.checkpoint("tool")
    ctx = dataclasses.replace(deps.tool_ctx, step=state.get("model_calls", 0))
    results = await run_tools(calls, ctx, set(deps.bound))
    messages = [
        ToolMessage(content=json.dumps(r, ensure_ascii=False, default=str), tool_call_id=c.id, name=c.name)
        for c, r in results
    ]
    return {"messages": messages, "tool_calls": made}


def route_after_step(state: CustomerState) -> str:
    return END if state.get("result") == "fallback" else "model"


def _tool_facts(state: CustomerState) -> list[str]:
    """This turn's tool results as the gate may trust them: our tools' data minus the customer's echoed words.
    Repair/nudge feedback (name "reply") is not a fact — it quotes the very amounts that were refused."""
    facts = []
    for m in state["messages"]:
        if isinstance(m, ToolMessage) and m.name != "reply":
            try:
                payload = json.loads(str(m.content))
            except ValueError:
                continue
            facts.append(json.dumps(without_customer_words(payload), ensure_ascii=False))
    return facts


async def gate_node(state: CustomerState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    ai = _last_ai(state)
    reply_call = next(c for c in _calls(ai) if c.name == "reply")
    # every other call in the same message gets a tool result, so the history stays well-formed for a repair round
    skipped = [
        ToolMessage(
            content='{"ok": false, "error": {"code": "NOT_EXECUTED", "message": "reply ended the turn"}}',
            tool_call_id=c.id,
            name=c.name,
        )
        for c in _calls(ai)
        if c.name != "reply"
    ]
    repaired = state.get("repair_attempted", False)
    try:
        output = AgentOutput.model_validate(reply_call.args)
    except ValidationError as exc:
        if repaired:
            return {"messages": skipped, "result": "fallback", "fallback_reason": "invalid_reply"}
        first = exc.errors()[0]
        msg = f"reply arguments invalid at {'.'.join(str(x) for x in first['loc'])}: {first['msg']}. Call reply again."
        return {
            "messages": [*skipped, ToolMessage(content=msg, tool_call_id=reply_call.id, name="reply")],
            "repair_attempted": True,
            "nudged": False,  # the repair round may forget the reply tool once too
        }
    p = deps.prepared
    customer = p.conversation.customer
    records = await grounding.hydrate(
        output,
        p.conversation.business_id,
        customer_id=customer.id if customer else None,
        customer_phone=customer.phone_e164 if customer else None,
        wa_display_number=deps.wa_display_number,
        trusted_texts=[p.trusted_facts, *_tool_facts(state)],
    )
    result: GateResult = output_gate.check(output, records)
    await emit(
        "gate",
        PipelineEventStatus.OK if result.passed else PipelineEventStatus.ERROR,
        business_id=p.conversation.business_id,
        conversation_id=p.conversation.id,
        turn_id=deps.tool_ctx.turn_id,
        duration_ms=int(result.duration_ms),
        **result.summary(),
    )
    update: dict[str, Any] = {"output": output.model_dump(mode="json"), "gate": result.model_dump(mode="json")}
    if result.passed:
        return update | {"messages": skipped, "result": "finalize"}
    if not repaired:
        repair = ToolMessage(content=result.repair_message(), tool_call_id=reply_call.id, name="reply")
        # live (kill/resume): the repair answer came as plain text and the turn fell back — one more nudge allowed
        return update | {"messages": [*skipped, repair], "repair_attempted": True, "nudged": False}
    return update | {"messages": skipped, "result": "fallback", "fallback_reason": "gate_failed"}


def route_after_gate(state: CustomerState) -> str:
    return END if state.get("result") in ("finalize", "fallback") else "model"


def build_graph() -> StateGraph[CustomerState]:
    g: StateGraph[CustomerState] = StateGraph(CustomerState)
    g.add_node("prepare", prepare_node)
    g.add_node("model", model_node)
    g.add_node("tools", tools_node)
    g.add_node("nudge", nudge_node)
    g.add_node("gate", gate_node)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "model")
    g.add_conditional_edges("model", route_after_model, ["gate", "tools", "nudge", END])
    g.add_conditional_edges("tools", route_after_step, ["model", END])
    g.add_conditional_edges("nudge", route_after_step, ["model", END])
    g.add_conditional_edges("gate", route_after_gate, ["model", END])
    return g
