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
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from reflexo.agent import metering
from reflexo.agent.model_gateway.config import ModelsConfig, RoleConfig
from reflexo.agent.model_gateway.providers import Attempt, ModelProvider
from reflexo.common.costs import Usage
from reflexo.common.enums import ModelCallKind, PipelineEventStatus
from reflexo.common.flags import flag
from reflexo.common.llm import CallCtx, ChatMessage, ChatResult, MediaInput, ModelError, ToolSpec
from reflexo.common.logging import get_logger
from reflexo.common.tracing import GENAI, tracer
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


def _kind(response_schema: dict[str, Any] | None, messages: list[ChatMessage]) -> ModelCallKind:
    if response_schema is None:
        return ModelCallKind.CHAT
    return ModelCallKind.VISION if any(m.media for m in messages) else ModelCallKind.STRUCTURED


@dataclass
class _Outcome:
    model: str
    usage: Usage
    duration_ms: int
    ttft_ms: int | None
    status: str
    error_code: str | None


@dataclass
class _CallMeter:
    """Every attempt of one gateway call that came back (the winner, a failed or an invalid one), written as
    `model_calls` rows once the call is over, so nothing is written from inside the racing attempts. A cancelled
    attempt reports no usage, so it has no row."""

    ctx: CallCtx
    role: str
    kind: ModelCallKind
    provider: str
    hedged: bool = False
    outcomes: list[_Outcome] = field(default_factory=list)

    def answered(self, result: ChatResult, *, error: str | None = None) -> None:
        usage = Usage(
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
        )
        status = "error" if error else "ok"
        self.outcomes.append(_Outcome(result.model, usage, result.latency_ms, result.ttft_ms, status, error))

    def failed(self, model: str, err: ModelError, duration_ms: int, *, status: str = "error") -> None:
        u = err.usage
        usage = Usage(
            input_tokens=u.get("input_tokens", 0),
            cached_input_tokens=u.get("cached_input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
        )
        self.outcomes.append(_Outcome(err.model or model, usage, duration_ms, None, status, err.code))

    async def write(self) -> int:
        """Write the rows; returns their total cost in micro-rupees (for the span)."""
        total = 0
        for o in self.outcomes:
            row = await metering.record(
                self.ctx,
                kind=self.kind,
                provider=self.provider,
                model=o.model,
                usage=o.usage,
                duration_ms=o.duration_ms,
                role=self.role,
                ttft_ms=o.ttft_ms,
                hedged=self.hedged,
                status=o.status,
                error_code=o.error_code,
            )
            total += row.cost_micro_inr if row else 0
        self.outcomes.clear()
        return total


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
        tool_choice: str | None = None,
    ) -> ChatResult:
        cfg = self.config.role(role)
        started = time.perf_counter()
        meter = _CallMeter(ctx, role, _kind(response_schema, messages), self.provider.name)
        operation = "chat" if response_schema is None else "generate_content"
        with tracer().start_as_current_span(f"{operation} {cfg.primary}") as span:
            span.set_attribute(GENAI.OPERATION, operation)
            span.set_attribute(GENAI.PROVIDER, GENAI.PROVIDER_NAMES.get(self.provider.name, self.provider.name))
            span.set_attribute(GENAI.REQUEST_MODEL, cfg.primary)
            span.set_attribute(GENAI.PURPOSE, ctx.purpose)
            span.set_attribute(GENAI.FEATURE, metering.feature_of(ctx))
            span.set_attribute("reflexo.model_role", role)
            if ctx.conversation_id:
                span.set_attribute(GENAI.CONVERSATION_ID, str(ctx.conversation_id))
            if ctx.prompt_version:
                span.set_attribute(GENAI.PROMPT_VERSION, ctx.prompt_version)
            try:
                result = await self._hedged(cfg, messages, tools, response_schema, validate, tool_choice, meter)
            except ModelError as err:
                duration_ms = int((time.perf_counter() - started) * 1000)
                if err.code == "TIMEOUT":  # the attempts were cancelled: their usage is unknown, the failure is not
                    meter.failed(cfg.primary, err, duration_ms, status="timeout")
                span.set_attribute("reflexo.error", err.code)
                span.set_attribute(GENAI.COST_MICRO_INR, await meter.write())
                await emit(
                    "model_call",
                    PipelineEventStatus.ERROR,
                    business_id=ctx.business_id,
                    conversation_id=ctx.conversation_id,
                    turn_id=ctx.turn_id,
                    duration_ms=duration_ms,
                    role=role,
                    purpose=ctx.purpose,
                    code=err.code,
                )
                log.warning("model_call_failed", role=role, purpose=ctx.purpose, code=err.code, model=err.model)
                raise
            span.set_attribute(GENAI.RESPONSE_MODEL, result.model)
            span.set_attribute(GENAI.INPUT_TOKENS, result.input_tokens)
            span.set_attribute(GENAI.OUTPUT_TOKENS, result.output_tokens)
            span.set_attribute(GENAI.CACHED_INPUT_TOKENS, result.cached_input_tokens)
            span.set_attribute(GENAI.REASONING_TOKENS, result.reasoning_tokens)
            span.set_attribute("reflexo.hedged", result.hedged)
            span.set_attribute(GENAI.COST_MICRO_INR, await meter.write())
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
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            tool_calls=[c.name for c in result.tool_calls],
        )
        return result

    async def _attempt(
        self, attempt: Attempt, validate: Validator | None, meter: _CallMeter | None = None
    ) -> ChatResult:
        started = time.perf_counter()
        try:
            result = await self.provider.generate(attempt)
        except ModelError as err:
            if meter is not None:
                meter.failed(attempt.model, err, int((time.perf_counter() - started) * 1000))
            raise
        if validate is not None:
            try:
                validate(result)
            except ModelError as err:  # a response that failed our schema is billed all the same
                if meter is not None:
                    meter.answered(result, error=err.code)
                raise
        if meter is not None:
            meter.answered(result)
        return result

    async def _hedged(
        self,
        cfg: RoleConfig,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        response_schema: dict[str, Any] | None,
        validate: Validator | None,
        tool_choice: str | None = None,
        meter: _CallMeter | None = None,
    ) -> ChatResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.timeout_ms / 1000
        primary = Attempt(
            cfg.primary,
            messages,
            tools,
            cfg,
            response_schema,
            pre_delay_s=await _chaos_delay_s(),
            tool_choice=tool_choice,
        )
        tasks: dict[asyncio.Task[ChatResult], str] = {
            asyncio.create_task(self._attempt(primary, validate, meter)): cfg.primary
        }
        fallback_started = False
        hedged = False
        errors: list[ModelError] = []

        def start_fallback() -> None:
            nonlocal fallback_started
            assert cfg.fallback is not None
            fallback_started = True
            attempt = Attempt(cfg.fallback, messages, tools, cfg, response_schema, tool_choice=tool_choice)
            tasks[asyncio.create_task(self._attempt(attempt, validate, meter))] = cfg.fallback

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
                    if meter is not None:
                        meter.hedged = True
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
        self,
        role: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        ctx: CallCtx,
        tool_choice: str | None = None,
    ) -> ChatResult:
        """`tool_choice`: None/"auto" lets the model answer in text; "any" requires a tool call; a tool name
        requires that tool. Agent graphs that must end in a tool (`reply`) pass "any"."""
        return await self._call(role, messages, tools, ctx, tool_choice=tool_choice)

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
        characters = sum(len(t) for t in texts)
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
        duration_ms = int((time.perf_counter() - started) * 1000)
        if ctx is not None:
            # The embeddings API reports no token count: tokens are taken as characters / 4 (Google's stated
            # average for its models, BETA_12 section 6.3); the characters are stored as well.
            await metering.record(
                ctx,
                kind=ModelCallKind.EMBED,
                provider=self.provider.name,
                model=cfg.model,
                usage=Usage(input_tokens=-(-characters // 4), characters=characters),
                duration_ms=duration_ms,
                feature="embedding",
            )
            await emit(
                "embedding",
                PipelineEventStatus.OK,
                business_id=ctx.business_id,
                duration_ms=duration_ms,
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

    async def embed(self, texts: list[str], *, task: str, ctx: CallCtx | None = None) -> list[list[float]]:
        return await self.gateway.embed(texts, task=task, ctx=ctx)


def tool_result(value: Any) -> str:
    """Serialize a tool result for a `tool` message (compact JSON; pydantic models supported)."""
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)
