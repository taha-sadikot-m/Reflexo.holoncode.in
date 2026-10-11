"""Provider-neutral LLM types shared by the model gateway and its users (POC_04 §2.2).

They live in `common` so knowledge, automations and owner can call the gateway through `ModelGatewayPort`
without importing the agent module.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]
ModelRole = Literal["customer_desk", "owner_desk", "builder", "vision", "extract", "classifier"]


class MediaInput(BaseModel):
    mime: str
    data: bytes | None = None  # inline bytes (images, PDFs ≤ ~15 MB)
    url: str | None = None  # or a fetchable URL (our signed media URLs)


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any]
    provider_meta: dict[str, Any] = {}  # e.g. Gemini thought signature (must be echoed back with the call)


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    media: list[MediaInput] = []
    tool_calls: list[ToolCall] = []  # assistant messages
    tool_call_id: str | None = None  # tool messages
    name: str | None = None  # tool messages: the tool name

    @classmethod
    def system(cls, text: str) -> ChatMessage:
        return cls(role="system", content=text)

    @classmethod
    def user(cls, text: str, media: list[MediaInput] | None = None) -> ChatMessage:
        return cls(role="user", content=text, media=media or [])

    @classmethod
    def assistant(cls, text: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatMessage:
        return cls(role="assistant", content=text, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, call: ToolCall, result: str) -> ChatMessage:
        return cls(role="tool", content=result, tool_call_id=call.id, name=call.name)


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (object)


class ChatResult(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = []
    model: str
    hedged: bool = False
    latency_ms: int = 0
    ttft_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None
    text_meta: dict[str, Any] = {}  # provider data for the text part (e.g. thought signature)


class ModelError(Exception):
    """Gateway failure with a registry code: TIMEOUT | RATE_LIMITED | PROVIDER_UNAVAILABLE | CREDENTIAL_INVALID |
    INVALID_OUTPUT | BLOCKED | PROVIDER_ERROR."""

    def __init__(self, code: str, message: str = "", *, model: str | None = None, retryable: bool = True) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.model = model
        self.retryable = retryable


@dataclass
class RunStats:
    """Accumulated over one desk invocation; written to `agent_runs` (POC_04 §12)."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    hedged: bool = False
    model_used: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)  # per call: model, latency, ttft, hedged, tokens

    def record(self, result: ChatResult, *, purpose: str) -> None:
        self.model_calls += 1
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.hedged = self.hedged or result.hedged
        self.model_used = result.model
        self.calls.append(
            {
                "purpose": purpose,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "ttft_ms": result.ttft_ms,
                "hedged": result.hedged,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            }
        )


@dataclass
class CallCtx:
    business_id: UUID | None
    purpose: str  # "customer_turn", "aliases", "media.describe", ...
    turn_id: UUID | None = None
    conversation_id: UUID | None = None
    stats: RunStats = field(default_factory=RunStats)


# The desk sets this before running its graph so the LangChain adapter can account calls to the right run.
current_call_ctx: contextvars.ContextVar[CallCtx | None] = contextvars.ContextVar("reflexo_call_ctx", default=None)
