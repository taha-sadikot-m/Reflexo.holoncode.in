"""Tool layer (POC_04 §3.1): registry, injected context, result envelope, idempotency ledger, schemas.

* The model only sees business arguments; `ToolContext` (ids, desk, channel, scopes) is injected by the tools node.
* Tools are bound per desk; calling an unbound tool returns `FORBIDDEN_SCOPE`.
* Side-effecting tools run inside a tenant transaction together with their `tool_effects` ledger row, so a replay
  after a crash returns the stored result instead of acting twice.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.flags import flag
from reflexo.common.llm import ToolCall, ToolSpec
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)


@dataclass(frozen=True)
class ToolContext:
    business_id: UUID
    desk: str
    channel: str
    conversation_id: UUID | None
    customer_id: UUID | None
    member_id: UUID | None
    turn_id: UUID | None
    call_session_id: UUID | None = None
    scopes: frozenset[str] = frozenset()
    step: int = 0
    call_index: int = 0
    extras: dict[str, Any] = field(default_factory=dict)  # e.g. customer phone, language

    def idem(self, suffix: str = "") -> str:
        base = (
            f"turn:{self.turn_id}:{self.step}:{self.call_index}"
            if self.turn_id
            else f"call:{self.call_session_id}:{self.step}:{self.call_index}"
        )
        return f"{base}:{suffix}" if suffix else base


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, guidance: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.guidance = guidance
        self.retryable = retryable


ReadFn = Callable[[ToolContext, Any], Awaitable[Any]]
EffectFn = Callable[[ToolContext, Any, AsyncSession], Awaitable[Any]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]
    desks: frozenset[str]
    side_effect: bool = False
    scopes: frozenset[str] = frozenset()
    terminal: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=model_schema(self.args))


TOOLS: dict[str, ToolDef] = {}


def tool(
    name: str,
    args: type[BaseModel],
    *,
    desks: set[str],
    side_effect: bool = False,
    scopes: set[str] | None = None,
    terminal: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        TOOLS[name] = ToolDef(
            name=name,
            description=(fn.__doc__ or "").strip(),
            args=args,
            fn=fn,
            desks=frozenset(desks),
            side_effect=side_effect,
            scopes=frozenset(scopes or ()),
            terminal=terminal,
        )
        return fn

    return deco


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def err(code: str, message: str, *, guidance: str | None = None, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable, "guidance": guidance}}


# ---------------------------------------------------------------- schemas for the model


_DROP = frozenset({"$defs", "discriminator", "title"})  # schema metadata only, never property names


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return {**_inline(copy.deepcopy(defs[name]), defs), **{k: v for k, v in node.items() if k != "$ref"}}
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _DROP:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {prop: _inline(sub, defs) for prop, sub in v.items()}  # a property may be called "title"
            else:
                out[k] = _inline(v, defs)
        return out
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def model_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema with `$ref`s inlined and pydantic-only keys removed (portable across providers)."""
    raw = model.model_json_schema()
    return dict(_inline(raw, raw.get("$defs", {})))


# ---------------------------------------------------------------- execution


# ---------------------------------------------------------------- explaining invalid arguments


def _tag(alt: dict[str, Any]) -> Any:
    """The union tag of one schema alternative (`"type": {"const": "buttons"}`), or None."""
    return ((alt.get("properties") or {}).get("type") or {}).get("const")


def _alternatives(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-null members of a union (pydantic writes discriminated unions as oneOf, optional values as anyOf)."""
    return [a for a in node.get("anyOf", node.get("oneOf", [])) if a.get("type") != "null"]


def _step(node: dict[str, Any], data: Any, part: str | int) -> tuple[dict[str, Any], Any, bool]:
    """Follow one element of a pydantic error location through the schema and the sent data.
    Returns (schema node, data node, whether `part` named a position in the data)."""
    alts = _alternatives(node)
    if alts and isinstance(part, str):
        tagged = next((a for a in alts if _tag(a) == part), None)
        if tagged is not None:  # discriminated union: pydantic puts the tag in the location
            return tagged, data, False
        node = next((a for a in alts if part in (a.get("properties") or {})), alts[0])
    if isinstance(part, int):
        return node.get("items") or {}, data[part] if isinstance(data, list) and part < len(data) else None, True
    props = node.get("properties") or {}
    sub = data.get(part) if isinstance(data, dict) else None
    if part in props:
        return props[part], sub, True
    if isinstance(node.get("additionalProperties"), dict):
        return node["additionalProperties"], sub, True
    return node, data, False  # a validator name in the location: not a position


def _shape(node: dict[str, Any]) -> str:
    """Compact picture of what a schema node accepts, e.g. {text: string, options: [{id: string, title: string}]}."""
    alts = _alternatives(node)
    if alts:
        tags = [_tag(a) for a in alts]
        if all(tags):
            return " | ".join(f'{{type: "{t}", ...}}' for t in tags)
        nullable = len(alts) < len(node.get("anyOf", node.get("oneOf", [])))
        return " | ".join(_shape(a) for a in alts) + (" | null" if nullable else "")
    if "const" in node:
        return json.dumps(node["const"], ensure_ascii=False)
    if "enum" in node:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in node["enum"])
    if "properties" in node:
        required = set(node.get("required", []))
        fields = [f"{k}{'' if k in required else '?'}: {_shape(v)}" for k, v in node["properties"].items()]
        return "{" + ", ".join(fields) + "}"
    if node.get("type") == "array":
        return f"[{_shape(node.get('items') or {})}]"
    if isinstance(node.get("additionalProperties"), dict):
        return f"{{string: {_shape(node['additionalProperties'])}}}"
    return str(node.get("type") or "any")


def explain_invalid(model: type[BaseModel], exc: ValidationError, args: Any) -> str:
    """Every problem in a tool call's arguments, grouped by the object it is in, with the shape each missing or
    wrong value should have and the keys that object does not know. One repair round can then fix them all
    (live: the repair named one missing field without the shape, and the model sent the same arguments again)."""
    schema = model_schema(model)
    groups: dict[str, dict[str, Any]] = {}
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        missing = error.get("type") == "missing"
        node, data, label = schema, args, ""
        for part in loc[:-1] if missing else loc:
            node, data, positional = _step(node, data, part)
            if positional:
                label += f"[{part}]" if isinstance(part, int) else (f".{part}" if label else str(part))
            elif _tag(node) is not None and not label.endswith(")"):
                label += f' (type "{_tag(node)}")'
        group = groups.setdefault(label, {"node": node, "data": data, "problems": []})
        if missing:
            field_name = str(loc[-1])
            field_node = (node.get("properties") or {}).get(field_name, {})
            group["problems"].append(f"missing {field_name} ({_shape(field_node)})")
        else:
            group["problems"].append(f"{error.get('msg')}; expected {_shape(node)}")
    lines = []
    for label, group in groups.items():
        problems = list(dict.fromkeys(group["problems"]))
        known = (group["node"].get("properties") or {}).keys()
        if known and isinstance(group["data"], dict):
            unknown = sorted(k for k in group["data"] if k not in known)
            if unknown:
                problems.append(f"not fields here: {', '.join(unknown)}")
        lines.append(f"- {label or 'arguments'}: " + "; ".join(problems))
    return "\n".join(lines)


async def run_tool(call: ToolCall, ctx: ToolContext, bound: set[str]) -> dict[str, Any]:
    """Validate, authorize and execute one call. Always returns an envelope; never raises for tool errors."""
    started = time.perf_counter()
    td = TOOLS.get(call.name)
    if td is None or call.name not in bound or ctx.desk not in td.desks:
        result = err("FORBIDDEN_SCOPE", f"tool {call.name} is not available here")
    elif td.scopes and not td.scopes <= ctx.scopes:
        result = err("FORBIDDEN_SCOPE", f"missing scope for {call.name}")
    else:
        try:
            args = td.args.model_validate(call.args)
        except ValidationError as exc:
            result = err(
                "INVALID_ARGS",
                "the arguments do not match the tool's schema",
                guidance="Fix every problem and call the tool again:\n" + explain_invalid(td.args, exc, call.args),
            )
        else:
            try:
                result = ok(await (_run_effect(td, ctx, args) if td.side_effect else td.fn(ctx, args)))
            except ToolError as exc:
                result = err(exc.code, str(exc), guidance=exc.guidance, retryable=exc.retryable)
            except Exception as exc:  # a tool bug must not kill the turn; the model gets a retryable error
                log.exception("tool_failed", tool=call.name)
                result = err(
                    "TOOL_FAILED", type(exc).__name__, retryable=True, guidance="Try once more or reply without it."
                )
    if result["ok"]:
        await _chaos_kill_after(call.name)
    await emit(
        "tool_call",
        PipelineEventStatus.OK if result["ok"] else PipelineEventStatus.ERROR,
        business_id=ctx.business_id,
        conversation_id=ctx.conversation_id,
        turn_id=ctx.turn_id,
        duration_ms=int((time.perf_counter() - started) * 1000),
        tool=call.name,
        code=None if result["ok"] else result["error"]["code"],
    )
    return result


async def _chaos_kill_after(tool_name: str) -> None:
    """Chaos "kill worker mid-reply" (POC_07 §5): a one-shot flag hard-kills this process right after the named tool
    finished — the turn must resume on another attempt from its checkpoint, without repeating side effects."""
    armed = await flag("kill_after_tool")
    if not isinstance(armed, dict) or armed.get("tool") != tool_name:
        return
    async with get_db().service_session(ServiceReason.HEALTH) as s:
        won = (await s.execute(text("DELETE FROM runtime_flags WHERE key = 'kill_after_tool' RETURNING key"))).first()
    if won is None:
        return  # another worker took it
    log.warning("chaos_kill_worker", tool=tool_name)
    os._exit(137)


async def _run_effect(td: ToolDef, ctx: ToolContext, args: BaseModel) -> Any:
    key = ctx.idem(td.name)
    async with get_db().tenant_session(ctx.business_id) as s:
        stored = (
            await s.execute(text("SELECT result FROM tool_effects WHERE idempotency_key = :k"), {"k": key})
        ).scalar_one_or_none()
        if stored is not None:
            log.info("tool_effect_replayed", tool=td.name, key=key)
            return stored
        data = await td.fn(ctx, args, s)
        inserted = (
            await s.execute(
                text("""INSERT INTO tool_effects (idempotency_key, business_id, tool, result)
                        VALUES (:k, :b, :t, CAST(:r AS jsonb)) ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING idempotency_key"""),
                {"k": key, "b": ctx.business_id, "t": td.name, "r": json.dumps(data, ensure_ascii=False, default=str)},
            )
        ).scalar_one_or_none()
        if inserted is None:  # a concurrent replay won: roll our effect back and return its result
            await s.rollback()
            async with get_db().tenant_session(ctx.business_id) as s2:
                return (
                    await s2.execute(text("SELECT result FROM tool_effects WHERE idempotency_key = :k"), {"k": key})
                ).scalar_one()
        return data


async def run_tools(calls: list[ToolCall], base: ToolContext, bound: set[str]) -> list[tuple[ToolCall, dict[str, Any]]]:
    """Read tools in parallel, side-effecting tools sequentially (in call order)."""
    results: dict[int, dict[str, Any]] = {}
    reads = [(i, c) for i, c in enumerate(calls) if not (TOOLS.get(c.name) and TOOLS[c.name].side_effect)]
    effects = [(i, c) for i, c in enumerate(calls) if TOOLS.get(c.name) and TOOLS[c.name].side_effect]

    def ctx_for(i: int) -> ToolContext:
        return dataclasses.replace(base, call_index=i)

    outs = await asyncio.gather(*(run_tool(c, ctx_for(i), bound) for i, c in reads))
    results.update({i: o for (i, _), o in zip(reads, outs, strict=True)})
    for i, c in effects:
        results[i] = await run_tool(c, ctx_for(i), bound)
    return [(c, results[i]) for i, c in enumerate(calls)]
