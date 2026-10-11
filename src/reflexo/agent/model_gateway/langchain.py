"""`GatewayChatModel`: a LangChain chat model backed by the gateway, for LangGraph desks (POC_04 §2.2).

Calls get hedging, failover, accounting and telemetry like any other gateway call. The run's `CallCtx` comes from
the `current_call_ctx` context variable that the desk sets before invoking its graph.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

from reflexo.agent.model_gateway.gateway import ModelGateway
from reflexo.common.llm import CallCtx, ChatMessage, MediaInput, ToolCall, ToolSpec, current_call_ctx

_META_KEY = "reflexo_provider_meta"


def _content_text(content: str | list[Any]) -> tuple[str, list[MediaInput]]:
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    media: list[MediaInput] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and block.get("type") == "media":
            media.append(MediaInput(mime=block["mime_type"], url=block.get("url"), data=block.get("data")))
    return "\n".join(texts), media


def to_gateway_messages(messages: Sequence[BaseMessage]) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    names: dict[str, str] = {}
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append(ChatMessage.system(_content_text(m.content)[0]))
        elif isinstance(m, HumanMessage):
            text, media = _content_text(m.content)
            out.append(ChatMessage.user(text, media))
        elif isinstance(m, AIMessage):
            meta = m.additional_kwargs.get(_META_KEY, {})
            calls = []
            for c in m.tool_calls:
                call_id = c.get("id") or ""
                names[call_id] = c["name"]
                calls.append(ToolCall(id=call_id, name=c["name"], args=c["args"], provider_meta=meta.get(call_id, {})))
            text, _ = _content_text(m.content)
            out.append(ChatMessage.assistant(text or None, calls))
        elif isinstance(m, ToolMessage):
            text, _ = _content_text(m.content)
            out.append(
                ChatMessage(
                    role="tool",
                    content=text,
                    tool_call_id=m.tool_call_id,
                    name=m.name or names.get(m.tool_call_id, "tool"),
                )
            )
        else:
            raise TypeError(f"unsupported message type {type(m).__name__}")
    return out


def _tool_spec(tool: dict[str, Any]) -> ToolSpec:
    fn = tool["function"] if "function" in tool else tool
    return ToolSpec(
        name=fn["name"],
        description=fn.get("description", ""),
        parameters=fn.get("parameters") or {"type": "object", "properties": {}},
    )


class GatewayChatModel(BaseChatModel):
    gateway: ModelGateway = Field(exclude=True)
    role: str
    purpose: str = "graph"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "reflexo-gateway"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"role": self.role}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("GatewayChatModel is async-only; use ainvoke/astream")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        ctx = current_call_ctx.get() or CallCtx(business_id=None, purpose=self.purpose)
        tools = [_tool_spec(t) for t in kwargs.get("tools") or []] or None
        result = await self.gateway.chat(
            self.role, to_gateway_messages(messages), tools, ctx=ctx, tool_choice=kwargs.get("tool_choice")
        )
        message = AIMessage(
            content=result.text,
            tool_calls=[{"name": c.name, "args": c.args, "id": c.id, "type": "tool_call"} for c in result.tool_calls],
            additional_kwargs={_META_KEY: {c.id: c.provider_meta for c in result.tool_calls if c.provider_meta}},
            response_metadata={
                "model_name": result.model,
                "hedged": result.hedged,
                "finish_reason": result.finish_reason,
            },
            usage_metadata={
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.input_tokens + result.output_tokens,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"model": result.model})
