"""Model gateway (POC_04 §2): one place for hedging, failover, timeouts, chaos, accounting and telemetry.

Policy per call:
1. Start the primary. If it has no first token after `hedge_after_ms`, start the fallback in parallel; the first
   *valid* result wins and the other attempt is cancelled.
2. If the primary fails (rate limit, 5xx, invalid output) before hedging, fail over to the fallback at once.
3. Hard timeout `timeout_ms` → `ModelError("TIMEOUT")`; the desk then uses its fallback path.
4. Chaos `runtime_flags.slow_model = {"delay_ms": N}` delays the primary attempt before the SDK call.
5. Each call is recorded on `ctx.stats` (→ `agent_runs`), as an OTel span `model_call` and an X-ray event.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from reflexo.agent.model_gateway.config import ModelsConfig, RoleConfig
from reflexo.agent.model_gateway.providers import Attempt, ModelProvider
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.flags import flag
from reflexo.common.llm import CallCtx, ChatMessage, ChatResult, MediaInput, ModelError, ToolSpec
from reflexo.common.logging import get_logger
from reflexo.common.tracing import tracer
from reflexo.common.xray import emit

log = get_logger(__name__)
M = TypeVar("M", bound=BaseModel)
Validator = Callable[[ChatResult], None]


async def _chaos_delay_s() -> float:
    try:
        value = await flag("slow_model")
    except Exception:  # flags need the DB; chaos must never break a call
        return 0.0
    if isinstance(value, dict):
        return float(value.get("delay_ms", 0)) / 1000
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) / 1000
    return 0.0


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


class ModelGateway:
    def __init__(self, provider: ModelProvider, config: ModelsConfig) -> None:
        self.provider = provider
        self.config = config

    # ---------------------------------------------------------------- core: one hedged call
    async def _call(
        self,
        role: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        ctx: CallCtx,
        *,
        response_schema: dict[str, Any] | None = None,
        validate: Validator | None = None,
    ) -> ChatResult:
        cfg = self.config.role(role)
        started = time.perf_counter()
        with tracer().start_as_current_span("model_call") as span:
            span.set_attribute("reflexo.model_role", role)
            span.set_attribute("reflexo.model", cfg.primary)
            span.set_attribute("reflexo.purpose", ctx.purpose)
            try:
                result = await self._hedged(cfg, messages, tools, response_schema, validate)
            except ModelError as err:
                span.set_attribute("reflexo.error", err.code)
                await emit(
                    "model_call",
                    PipelineEventStatus.ERROR,
                    business_id=ctx.business_id,
                    conversation_id=ctx.conversation_id,
                    turn_id=ctx.turn_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    role=role,
                    purpose=ctx.purpose,
                    code=err.code,
                )
                log.warning("model_call_failed", role=role, purpose=ctx.purpose, code=err.code, model=err.model)
                raise
            span.set_attribute("reflexo.model_used", result.model)
            span.set_attribute("reflexo.hedged", result.hedged)
            span.set_attribute("reflexo.tokens.input", result.input_tokens)
            span.set_attribute("reflexo.tokens.output", result.output_tokens)
        ctx.stats.record(result, purpose=ctx.purpose)
        await emit(
            "model_call",
            PipelineEventStatus.OK,
            business_id=ctx.business_id,
            conversation_id=ctx.conversation_id,
            turn_id=ctx.turn_id,
            duration_ms=result.latency_ms,
            role=role,
            purpose=ctx.purpose,
            model=result.model,
            hedged=result.hedged,
            ttft_ms=result.ttft_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tool_calls=[c.name for c in result.tool_calls],
        )
        return result

    async def _attempt(self, attempt: Attempt, validate: Validator | None) -> ChatResult:
        result = await self.provider.generate(attempt)
        if validate is not None:
            validate(result)
        return result

    async def _hedged(
        self,
        cfg: RoleConfig,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        response_schema: dict[str, Any] | None,
        validate: Validator | None,
    ) -> ChatResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.timeout_ms / 1000
        primary = Attempt(cfg.primary, messages, tools, cfg, response_schema, pre_delay_s=await _chaos_delay_s())
        tasks: dict[asyncio.Task[ChatResult], str] = {
            asyncio.create_task(self._attempt(primary, validate)): cfg.primary
        }
        fallback_started = False
        hedged = False
        errors: list[ModelError] = []

        def start_fallback() -> None:
            nonlocal fallback_started
            assert cfg.fallback is not None
            fallback_started = True
            attempt = Attempt(cfg.fallback, messages, tools, cfg, response_schema)
            tasks[asyncio.create_task(self._attempt(attempt, validate))] = cfg.fallback

        try:
            if cfg.fallback and cfg.hedge_after_ms is not None:
                first = asyncio.create_task(primary.first_token.wait())
                try:
                    await asyncio.wait(
                        [first, *tasks],
                        timeout=min(cfg.hedge_after_ms / 1000, max(0.0, deadline - loop.time())),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    first.cancel()
                primary_task = next(iter(tasks))
                if not primary.first_token.is_set() and not primary_task.done():
                    start_fallback()
                    hedged = True
            while tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ModelError("TIMEOUT", f"no valid answer within {cfg.timeout_ms} ms", model=cfg.primary)
                done, _ = await asyncio.wait(list(tasks), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise ModelError("TIMEOUT", f"no valid answer within {cfg.timeout_ms} ms", model=cfg.primary)
                for task in done:
                    model = tasks.pop(task)
                    exc = task.exception()
                    if exc is None:
                        return task.result().model_copy(update={"hedged": hedged})
                    err = exc if isinstance(exc, ModelError) else ModelError("PROVIDER_ERROR", repr(exc), model=model)
                    errors.append(err)
                    log.info("model_attempt_failed", model=model, code=err.code)
                    if cfg.fallback and not fallback_started and model == cfg.primary and err.retryable:
                        start_fallback()  # failover: the primary failed before we hedged
            raise errors[-1] if errors else ModelError("PROVIDER_ERROR", "no attempt ran")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    # ---------------------------------------------------------------- public API (ModelGatewayPort)
    async def chat(
        self, role: str, messages: list[ChatMessage], tools: list[ToolSpec] | None = None, *, ctx: CallCtx
    ) -> ChatResult:
        return await self._call(role, messages, tools, ctx)

    async def structured(self, role: str, messages: list[ChatMessage], schema: type[M], *, ctx: CallCtx) -> M:
        """JSON-mode call validated against `schema`. An invalid answer counts as a failed attempt (so the
        fallback can win); if every attempt is invalid, one repair round tells the model what was wrong."""
        parsed: dict[str, M] = {}

        def validate(result: ChatResult) -> None:
            try:
                parsed[result.model] = schema.model_validate_json(result.text)
            except (ValidationError, ValueError) as exc:
                raise ModelError("INVALID_OUTPUT", str(exc)[:500], model=result.model) from exc

        json_schema = _schema_for(schema)
        try:
            result = await self._call(role, messages, None, ctx, response_schema=json_schema, validate=validate)
        except ModelError as err:
            if err.code != "INVALID_OUTPUT":
                raise
            repair = [
                *messages,
                ChatMessage.user(
                    "Your previous answer was not valid JSON for the required schema "
                    f"({str(err)[:300]}). Reply again with only valid JSON matching the schema."
                ),
            ]
            result = await self._call(role, repair, None, ctx, response_schema=json_schema, validate=validate)
        return parsed[result.model]

    async def vision(self, role: str, prompt: str, media: list[MediaInput], schema: type[M], *, ctx: CallCtx) -> M:
        return await self.structured(role, [ChatMessage.user(prompt, media=media)], schema, ctx=ctx)

    async def embed(self, texts: list[str], *, task: str, ctx: CallCtx | None = None) -> list[list[float]]:
        """Batched embeddings (`task`: "query" | "document"), normalized to unit length."""
        cfg = self.config.embedding
        started = time.perf_counter()
        out: list[list[float]] = []
        for i in range(0, len(texts), cfg.batch_size):
            batch = texts[i : i + cfg.batch_size]
            for attempt in range(2):  # one retry for transient errors
                try:
                    vectors = await asyncio.wait_for(
                        self.provider.embed(cfg.model, batch, task=task, dimensions=cfg.dimensions),
                        cfg.timeout_ms / 1000,
                    )
                    break
                except TimeoutError as exc:
                    if attempt:
                        raise ModelError("TIMEOUT", "embedding timed out", model=cfg.model) from exc
                except ModelError as err:
                    if attempt or not err.retryable:
                        raise
            out += [_unit(v) for v in vectors]
        if ctx is not None:
            await emit(
                "embedding",
                PipelineEventStatus.OK,
                business_id=ctx.business_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                purpose=ctx.purpose,
                count=len(texts),
            )
        return out


def _unit(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm else vec


class GatewayEmbedder:
    """EmbedderPort for knowledge search."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway
        self.model = gateway.config.embedding.model

    async def embed(self, texts: list[str], *, task: str) -> list[list[float]]:
        return await self.gateway.embed(texts, task=task)


def tool_result(value: Any) -> str:
    """Serialize a tool result for a `tool` message (compact JSON; pydantic models supported)."""
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)
