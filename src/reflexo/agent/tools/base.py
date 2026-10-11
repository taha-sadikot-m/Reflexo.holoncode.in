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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
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


def _guidance_for(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"Fix argument '{where}': {first.get('msg')}"


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
            result = err("INVALID_ARGS", str(exc.errors()[0].get("msg")), guidance=_guidance_for(exc))
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
