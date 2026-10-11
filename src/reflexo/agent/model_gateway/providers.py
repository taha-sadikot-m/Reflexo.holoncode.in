"""Model providers behind the gateway: Gemini (google-genai SDK) and a scripted fake for tests/evals/offline dev.

A provider performs exactly one attempt against one model. Hedging, failover, timeouts, chaos and accounting
live in the gateway, so every provider gets them for free.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from reflexo.agent.model_gateway.config import RoleConfig
from reflexo.common.ids import new_id
from reflexo.common.llm import ChatMessage, ChatResult, MediaInput, ModelError, ToolCall, ToolSpec


@dataclass
class Attempt:
    """One provider call. `first_token` is set when the first chunk arrives (drives hedging)."""

    model: str
    messages: list[ChatMessage]
    tools: list[ToolSpec] | None
    cfg: RoleConfig
    response_schema: dict[str, Any] | None = None
    first_token: asyncio.Event = field(default_factory=asyncio.Event)
    pre_delay_s: float = 0.0  # chaos: injected before the SDK call
    tool_choice: str | None = None  # None/"auto" | "any" (must call some tool) | a tool name (must call it)


class ModelProvider(Protocol):
    name: str

    async def generate(self, attempt: Attempt) -> ChatResult: ...

    async def embed(self, model: str, texts: list[str], *, task: str, dimensions: int) -> list[list[float]]: ...


# ---------------------------------------------------------------- Gemini


def _b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data else None


# Validation-only keywords. Gemini compiles tool schemas into a constrained-decoding grammar; with these kept the
# customer tool set fails in ANY mode ("too many states for serving") and the model drifts into prose in AUTO mode.
# Our own Pydantic validation of every tool call stays authoritative (invalid args → repair round).
_VALIDATION_ONLY = frozenset(
    {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
        "default",
        "title",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "uniqueItems",
    }
)


def lean_schema(node: Any) -> Any:
    """The model-facing projection of a JSON schema: structure, types, enums, consts, required, descriptions."""
    if isinstance(node, list):
        return [lean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: lean_schema(sub) for name, sub in value.items()}  # property names are data
        elif key in _VALIDATION_ONLY:
            continue
        else:
            out["anyOf" if key == "oneOf" else key] = lean_schema(value)
    # A const property is a union tag ("type": "text"). Pydantic models give it a default, so it is not required —
    # without the default in view the model left it out and the discriminated union rejected the block (live).
    tags = [k for k, v in out.get("properties", {}).items() if isinstance(v, dict) and "const" in v]
    if tags:
        out["required"] = [*out.get("required", []), *(t for t in tags if t not in out.get("required", []))]
    return out


class GeminiProvider:
    """Gemini via google-genai (async, streaming). SDK retries are disabled: the gateway owns failover."""

    name = "gemini"

    def __init__(self, api_key: str) -> None:
        from google import genai
        from google.genai import types

        if not api_key:
            raise ModelError("CREDENTIAL_INVALID", "REFLEXO_GOOGLE_API_KEY is not set", retryable=False)
        self._types = types
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
        )

    # -------------------------------------------------------- request building
    def _media_part(self, m: MediaInput) -> Any:
        t = self._types
        if m.data is not None:
            return t.Part.from_bytes(data=m.data, mime_type=m.mime)
        if m.url:
            return t.Part.from_uri(file_uri=m.url, mime_type=m.mime)
        raise ModelError("PROVIDER_ERROR", "media input needs data or url", retryable=False)

    def _contents(self, messages: list[ChatMessage]) -> tuple[str | None, list[Any]]:
        t = self._types
        system = "\n\n".join(m.content or "" for m in messages if m.role == "system") or None
        contents: list[Any] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "user":
                parts = [t.Part.from_text(text=m.content or "")] if m.content else []
                parts += [self._media_part(x) for x in m.media]
                contents.append(t.Content(role="user", parts=parts or [t.Part.from_text(text=" ")]))
            elif m.role == "assistant":
                parts = []
                if m.content:
                    parts.append(t.Part.from_text(text=m.content))
                for c in m.tool_calls:
                    sig = c.provider_meta.get("thought_signature")
                    parts.append(
                        t.Part(
                            function_call=t.FunctionCall(id=c.id, name=c.name, args=c.args),
                            thought_signature=base64.b64decode(sig) if sig else None,
                        )
                    )
                contents.append(t.Content(role="model", parts=parts or [t.Part.from_text(text=" ")]))
            else:  # tool result → a user turn carrying the function response
                try:
                    payload = json.loads(m.content or "null")
                except ValueError:
                    payload = m.content
                response = payload if isinstance(payload, dict) else {"result": payload}
                part = t.Part(function_response=t.FunctionResponse(id=m.tool_call_id, name=m.name, response=response))
                if contents and contents[-1].role == "user" and contents[-1].parts[0].function_response is not None:
                    contents[-1].parts.append(part)  # parallel calls: all responses in one turn
                else:
                    contents.append(t.Content(role="user", parts=[part]))
        return system, contents

    def _config(self, attempt: Attempt, system: str | None) -> Any:
        t = self._types
        cfg = attempt.cfg
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": cfg.temperature,
            "max_output_tokens": cfg.max_output_tokens,
            "automatic_function_calling": t.AutomaticFunctionCallingConfig(disable=True),
        }
        if cfg.thinking_budget is not None:
            kwargs["thinking_config"] = t.ThinkingConfig(thinking_budget=cfg.thinking_budget)
        if attempt.tools:
            kwargs["tools"] = [
                t.Tool(
                    function_declarations=[
                        t.FunctionDeclaration(
                            name=s.name, description=s.description, parameters_json_schema=lean_schema(s.parameters)
                        )
                        for s in attempt.tools
                    ]
                )
            ]
            if attempt.tool_choice and attempt.tool_choice != "auto":
                only = None if attempt.tool_choice == "any" else [attempt.tool_choice]
                kwargs["tool_config"] = t.ToolConfig(
                    function_calling_config=t.FunctionCallingConfig(
                        mode=t.FunctionCallingConfigMode.ANY, allowed_function_names=only
                    )
                )
        if attempt.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_json_schema"] = attempt.response_schema
        return t.GenerateContentConfig(**kwargs)

    # -------------------------------------------------------- calls
    def _map_error(self, exc: Exception, model: str) -> ModelError:
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            code = int(getattr(exc, "code", 0) or 0)
            if code == 429:
                return ModelError("RATE_LIMITED", str(exc)[:300], model=model)
            if code in (401, 403):
                return ModelError("CREDENTIAL_INVALID", str(exc)[:300], model=model, retryable=False)
            if code >= 500:
                return ModelError("PROVIDER_UNAVAILABLE", str(exc)[:300], model=model)
            return ModelError("PROVIDER_ERROR", str(exc)[:300], model=model, retryable=code == 404)
        return ModelError("PROVIDER_UNAVAILABLE", f"{type(exc).__name__}: {str(exc)[:200]}", model=model)

    async def generate(self, attempt: Attempt) -> ChatResult:
        if attempt.pre_delay_s:
            await asyncio.sleep(attempt.pre_delay_s)
        system, contents = self._contents(attempt.messages)
        config = self._config(attempt, system)
        started = time.perf_counter()
        ttft: int | None = None
        texts: list[str] = []
        text_meta: dict[str, Any] = {}
        calls: list[ToolCall] = []
        usage: Any = None
        finish: str | None = None
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=attempt.model, contents=contents, config=config
            )
            async for chunk in stream:
                if ttft is None:
                    ttft = int((time.perf_counter() - started) * 1000)
                    attempt.first_token.set()
                usage = chunk.usage_metadata or usage
                for cand in chunk.candidates or []:
                    if cand.finish_reason is not None:
                        finish = str(
                            cand.finish_reason.value if hasattr(cand.finish_reason, "value") else cand.finish_reason
                        )
                    for part in cand.content.parts if cand.content and cand.content.parts else []:
                        if part.thought:
                            continue
                        if part.function_call is not None:
                            fc = part.function_call
                            meta = {"thought_signature": _b64(part.thought_signature)} if part.thought_signature else {}
                            calls.append(
                                ToolCall(
                                    id=fc.id or f"call_{new_id().hex[-12:]}",
                                    name=fc.name or "",
                                    args=dict(fc.args or {}),
                                    provider_meta=meta,
                                )
                            )
                        elif part.text:
                            texts.append(part.text)
                            if part.thought_signature:
                                text_meta["thought_signature"] = _b64(part.thought_signature)
        except ModelError:
            raise
        except Exception as exc:
            raise self._map_error(exc, attempt.model) from exc
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise ModelError("BLOCKED", f"finish_reason={finish}", model=attempt.model, retryable=False)
        if not texts and not calls:
            raise ModelError("INVALID_OUTPUT", f"empty response (finish_reason={finish})", model=attempt.model)
        return ChatResult(
            text="".join(texts),
            tool_calls=calls,
            model=attempt.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ttft_ms=ttft,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0)
            + int(getattr(usage, "thoughts_token_count", 0) or 0),
            finish_reason=finish,
            text_meta=text_meta,
        )

    async def embed(self, model: str, texts: list[str], *, task: str, dimensions: int) -> list[list[float]]:
        t = self._types
        task_type = "RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT"
        try:
            result = await self._client.aio.models.embed_content(
                model=model,
                contents=texts,
                config=t.EmbedContentConfig(output_dimensionality=dimensions, task_type=task_type),
            )
        except Exception as exc:
            raise self._map_error(exc, model) from exc
        vectors = [list(e.values or []) for e in (result.embeddings or [])]
        if len(vectors) != len(texts) or any(len(v) != dimensions for v in vectors):
            raise ModelError("INVALID_OUTPUT", "embedding count/dimension mismatch", model=model)
        return vectors


# ---------------------------------------------------------------- fake (tests, evals, offline dev)


@dataclass
class FakeReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    first_token_ms: int = 0
    total_ms: int = 0
    error: ModelError | None = None
    input_tokens: int = 10
    output_tokens: int = 5


Script = Callable[[Attempt], FakeReply | Awaitable[FakeReply]]


class FakeProvider:
    """Deterministic provider. Configure per model: a fixed reply, a queue of replies, or a function."""

    name = "fake"

    def __init__(self) -> None:
        self.scripts: dict[str, Script] = {}
        self.queues: dict[str, list[FakeReply]] = {}
        self.calls: list[Attempt] = []
        self.cancelled: list[str] = []

    def reply(self, model: str, reply: FakeReply | str) -> None:
        r = FakeReply(text=reply) if isinstance(reply, str) else reply
        self.scripts[model] = lambda _a: r

    def queue(self, model: str, *replies: FakeReply | str) -> None:
        self.queues.setdefault(model, []).extend(FakeReply(text=r) if isinstance(r, str) else r for r in replies)

    def script(self, model: str, fn: Script) -> None:
        self.scripts[model] = fn

    async def _resolve(self, attempt: Attempt) -> FakeReply:
        if self.queues.get(attempt.model):
            return self.queues[attempt.model].pop(0)
        fn = self.scripts.get(attempt.model) or self.scripts.get("*")
        if fn is None:
            last_user = next((m.content for m in reversed(attempt.messages) if m.role == "user"), "")
            return FakeReply(text=f"echo: {last_user}")
        out = fn(attempt)
        return await out if asyncio.iscoroutine(out) else out  # type: ignore[return-value]

    async def generate(self, attempt: Attempt) -> ChatResult:
        self.calls.append(attempt)
        started = time.perf_counter()
        try:
            if attempt.pre_delay_s:
                await asyncio.sleep(attempt.pre_delay_s)
            r = await self._resolve(attempt)
            await asyncio.sleep(r.first_token_ms / 1000)
            if r.error is not None:
                raise r.error
            attempt.first_token.set()
            ttft = int((time.perf_counter() - started) * 1000)
            await asyncio.sleep(max(0, r.total_ms - r.first_token_ms) / 1000)
        except asyncio.CancelledError:
            self.cancelled.append(attempt.model)
            raise
        if attempt.response_schema is not None and not r.tool_calls:
            json.loads(r.text or "null")  # fake honours JSON mode loosely; the gateway validates the schema
        return ChatResult(
            text=r.text,
            tool_calls=r.tool_calls,
            model=attempt.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ttft_ms=ttft,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            finish_reason="STOP",
        )

    async def embed(self, model: str, texts: list[str], *, task: str, dimensions: int) -> list[list[float]]:
        import hashlib
        import math

        out = []
        for text in texts:
            vec = [0.0] * dimensions
            for word in text.lower().split():
                vec[int(hashlib.sha1(word.encode()).hexdigest(), 16) % dimensions] += 1.0  # noqa: S324 - not security
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out
