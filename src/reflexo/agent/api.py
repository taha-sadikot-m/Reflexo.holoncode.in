"""Public interface of the `agent` module. Other modules import only from here (POC_01 §4).

LangGraph desks (customer, owner, builder), tools, output gate, ask-owner, model gateway. Spec: POC_04.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from reflexo.agent import cost_jobs, grounding, media_describe, metering, output_gate, sandbox
from reflexo.agent.capabilities import CAPABILITIES
from reflexo.agent.desk import CustomerDesk
from reflexo.agent.model_gateway.config import ModelsConfig, RoleConfig, load_models_config
from reflexo.agent.model_gateway.gateway import GatewayEmbedder, ModelGateway, tool_result
from reflexo.agent.model_gateway.langchain import GatewayChatModel, to_gateway_messages
from reflexo.agent.model_gateway.providers import Attempt, FakeProvider, FakeReply, GeminiProvider, ModelProvider
from reflexo.agent.output_gate import extract_amounts
from reflexo.agent.proposals import ProposalService
from reflexo.agent.questions import SLA_QUEUE, QuestionService, ask_owner, on_record_since
from reflexo.agent.runs import record_run
from reflexo.agent.schemas import AgentOutput, OwnerQuestionDraft
from reflexo.agent.tools.base import ToolContext, run_tool
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import (
    EmbedderPort,
    JobHandlerRegistry,
    JobRow,
    ModelGatewayPort,
    ProposalPort,
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
    "record_usage",
    "register",
    "spent_today",
    "to_gateway_messages",
    "tool_result",
    "turn_cost",
]

# metering (BETA_12 §7): voice usage blocks and transcriptions are recorded through here
record_usage = metering.record
spent_today = metering.spent_today
turn_cost = metering.turn_cost

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
    registry.provide(ProposalPort, ProposalService(registry))  # type: ignore[type-abstract]
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

        async def fx_refresh(job: JobRow) -> None:
            await cost_jobs.fx_refresh()

        async def cost_rollup(job: JobRow) -> None:
            await cost_jobs.rollup()

        jobs.register_daily(cost_jobs.FX_QUEUE, fx_refresh, get_settings().fx_refresh_at)
        jobs.register_daily(cost_jobs.ROLLUP_QUEUE, cost_rollup, cost_jobs.ROLLUP_AT)


def gateway() -> ModelGateway:
    if _gateway is None:
        raise RuntimeError("agent not registered (bootstrap() not called)")
    return _gateway


def langchain_model(role: str, purpose: str = "graph") -> GatewayChatModel:
    return GatewayChatModel(gateway=gateway(), role=role, purpose=purpose)


__all__ += ["ChatResult", "GeminiProvider", "MediaInput", "ModelError", "ModelProvider", "langchain_model"]

# Shared tools the voice desk binds (POC_04 §10.4); voice-only tools (end_call, send_to_chat...) live in voice.
VOICE_TOOLS = (
    "catalog_search",
    "catalog_get",
    "knowledge_lookup",
    "delivery_quote",
    "order_start",
    "order_update",
    "order_confirm",
    "order_status",
    # one employee on every channel: the caller can ask for what a chat customer can (ADR-015), and the agent
    # can ask the owner — a voice promise is always backed by a record (live 14 Oct: "I'll confirm with the
    # owner" on a call, with no way to, and nothing reached the owner)
    "order_cancel",
    "order_change",
    "refund_request",
    "ask_owner",
)
__all__ += ["VOICE_TOOLS", "ToolContext", "extract_amounts", "run_tool"]


async def preview_answer(business_id: UUID, question: str) -> str:
    """Onboarding preview: how the customer desk would answer, without a conversation or any side effect."""
    return await sandbox.preview_answer(gateway(), business_id, question)


__all__ += ["preview_answer"]


async def describe_media(business_id: UUID, media_object_id: UUID, *, owner_file: bool = False) -> str:
    """One-line description of a stored photo/PDF/sheet (vision role; also saved on the media object)."""
    return await media_describe.describe(gateway(), business_id, media_object_id, owner_file=owner_file)


__all__ += ["describe_media"]


__all__ += ["CAPABILITIES"]  # risk-classed actions (ADR-015): name → label, verb, default authority


async def decide_answer_scope(business_id: UUID, question: str, answer: str) -> str:
    """Who an owner's answer applies to — global | customer | temporary (the ask-owner loop's own decider)."""
    scope, _ = await QuestionService(ports.registry, gateway()).decide_scope(business_id, question, answer, None)
    return scope


async def generalise_decision(
    business_id: UUID, capability: str, situation: dict[str, Any], reason: str, approve: bool
) -> dict[str, Any]:
    """The rule the learning loop would offer after the owner decided (effect + conditions that hold)."""
    sub = {"situation": situation, "reason": reason}
    return await ProposalService(ports.registry).generalise(business_id, CAPABILITIES[capability], sub, approve)


__all__ += ["decide_answer_scope", "generalise_decision"]  # used by the eval suites (POC_08 §4)


async def gate_check(
    business_id: UUID, output: dict[str, Any], *, trusted_texts: tuple[str, ...] = ()
) -> tuple[bool, list[str]]:
    """Run the real output gate on a given reply (hydrated against the business's data, as in a turn):
    (passed, names of the failed checks). For the adversarial eval suite."""
    parsed = AgentOutput.model_validate(output)
    records = await grounding.hydrate(
        parsed, business_id, customer_id=None, customer_phone=None, trusted_texts=trusted_texts
    )
    result = output_gate.check(parsed, records)
    return result.passed, [c.name for c in result.failures]


__all__ += ["gate_check"]


__all__ += [
    "OwnerQuestionDraft",
    "ask_owner",
    "on_record_since",
]  # any desk asks the owner the same way; what is on record for a chat

from reflexo.agent.language import default_language, fixed, language_rule, reply_language  # noqa: E402
from reflexo.agent.language import detect as detect_language  # noqa: E402

__all__ += [
    "default_language",
    "detect_language",
    "fixed",
    "language_rule",
    "reply_language",
]  # the platform's language policy (en | auto)
