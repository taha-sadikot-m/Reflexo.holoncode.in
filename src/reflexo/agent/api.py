"""Public interface of the `agent` module. Other modules import only from here (POC_01 §4).

LangGraph desks (customer, owner, builder), tools, output gate, ask-owner, model gateway. Spec: POC_04.
"""

from __future__ import annotations

from uuid import UUID

from reflexo.agent import media_describe
from reflexo.agent.desk import CustomerDesk
from reflexo.agent.model_gateway.config import ModelsConfig, RoleConfig, load_models_config
from reflexo.agent.model_gateway.gateway import GatewayEmbedder, ModelGateway, tool_result
from reflexo.agent.model_gateway.langchain import GatewayChatModel, to_gateway_messages
from reflexo.agent.model_gateway.providers import Attempt, FakeProvider, FakeReply, GeminiProvider, ModelProvider
from reflexo.agent.output_gate import extract_amounts
from reflexo.agent.questions import SLA_QUEUE, QuestionService
from reflexo.agent.runs import record_run
from reflexo.agent.tools.base import ToolContext, run_tool
from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    EmbedderPort,
    JobHandlerRegistry,
    JobRow,
    ModelGatewayPort,
    QuestionAnswerPort,
    TurnRunnerPort,
)
from reflexo.common.llm import ChatResult, MediaInput, ModelError
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry

__all__ = [
    "Attempt",
    "FakeProvider",
    "FakeReply",
    "GatewayChatModel",
    "ModelGateway",
    "ModelsConfig",
    "RoleConfig",
    "gateway",
    "load_models_config",
    "record_run",
    "register",
    "to_gateway_messages",
    "tool_result",
]

log = get_logger(__name__)
_gateway: ModelGateway | None = None


class _UnconfiguredProvider:
    """Stands in when no API key is set: every call fails clearly instead of the process refusing to start."""

    name = "unconfigured"

    async def generate(self, attempt: Attempt) -> ChatResult:
        raise ModelError(
            "CREDENTIAL_INVALID", "REFLEXO_GOOGLE_API_KEY is not set", model=attempt.model, retryable=False
        )

    async def embed(self, model: str, texts: list[str], *, task: str, dimensions: int) -> list[list[float]]:
        raise ModelError("CREDENTIAL_INVALID", "REFLEXO_GOOGLE_API_KEY is not set", model=model, retryable=False)


def build_provider() -> ModelProvider:
    s = get_settings()
    if s.model_provider == "fake":
        return FakeProvider()
    key = s.google_api_key.get_secret_value()
    if not key:
        log.warning("model_provider_unconfigured", hint="set REFLEXO_GOOGLE_API_KEY or REFLEXO_MODEL_PROVIDER=fake")
        return _UnconfiguredProvider()
    return GeminiProvider(key)


def register(registry: Registry, provider: ModelProvider | None = None) -> None:
    """Provide the model gateway (and the embedder for knowledge search when a provider is configured)."""
    global _gateway
    provider = provider or build_provider()
    _gateway = ModelGateway(provider, load_models_config())
    registry.provide(ModelGatewayPort, _gateway)  # type: ignore[type-abstract]
    if not isinstance(provider, _UnconfiguredProvider):
        registry.provide(EmbedderPort, GatewayEmbedder(_gateway))  # type: ignore[type-abstract]
    registry.provide(TurnRunnerPort, CustomerDesk(registry, _gateway))  # type: ignore[type-abstract]
    questions = QuestionService(registry, _gateway)
    registry.provide(QuestionAnswerPort, questions)  # type: ignore[type-abstract]
    jobs = registry.optional(JobHandlerRegistry)  # type: ignore[type-abstract]
    if jobs is not None:

        async def owner_question_sla(job: JobRow) -> None:
            await questions.run_sla(UUID(job.payload["business_id"]), UUID(job.payload["question_id"]))

        jobs.register(SLA_QUEUE, owner_question_sla)
        model_gateway = _gateway

        async def describe_media(job: JobRow) -> None:
            p = job.payload
            await media_describe.describe(
                model_gateway, UUID(p["business_id"]), UUID(p["media_object_id"]), owner_file=bool(p.get("owner"))
            )

        jobs.register(media_describe.QUEUE, describe_media)


def gateway() -> ModelGateway:
    if _gateway is None:
        raise RuntimeError("agent not registered (bootstrap() not called)")
    return _gateway


def langchain_model(role: str, purpose: str = "graph") -> GatewayChatModel:
    return GatewayChatModel(gateway=gateway(), role=role, purpose=purpose)


__all__ += ["ChatResult", "GeminiProvider", "MediaInput", "ModelError", "ModelProvider", "langchain_model"]

# Shared tools the voice desk binds (POC_04 §10.4); voice-only tools (end_call, send_to_chat...) live in voice.
VOICE_TOOLS = ("catalog_search", "catalog_get", "knowledge_lookup", "delivery_quote")
__all__ += ["VOICE_TOOLS", "ToolContext", "extract_amounts", "run_tool"]
