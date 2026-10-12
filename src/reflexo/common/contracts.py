"""Cross-module contracts: port protocols and the value types they exchange (POC_01 §4.2, POC_03).

Ports are provided by the owning module at bootstrap and required by consumers, so the pipeline can drive
gateway/agent/owner code without importing those modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.enums import Desk, PriorityClass, TurnOutcome
from reflexo.common.llm import CallCtx, ChatMessage, ChatResult, MediaInput, ToolSpec

_M = TypeVar("_M", bound=BaseModel)

# ---------------------------------------------------------------- rows handed to port implementations


@dataclass(frozen=True)
class InboxEvent:
    id: UUID
    provider: str
    route_key: str | None
    dedupe_key: str
    headers: dict[str, Any]
    payload: dict[str, Any]
    simulated: bool
    attempts: int
    trace_context: dict[str, Any] | None


@dataclass(frozen=True)
class OutboundRow:
    id: UUID
    business_id: UUID
    message_id: UUID
    conversation_id: UUID
    channel_connection_id: UUID
    part_index: int
    idempotency_key: str
    rendered_payload: dict[str, Any]
    attempts: int
    status: str
    provider_message_id: str | None
    trace_context: dict[str, Any] | None


@dataclass(frozen=True)
class JobRow:
    id: UUID
    business_id: UUID | None
    queue: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    trace_context: dict[str, Any] | None


class SendStatus(StrEnum):
    SENT = "sent"
    RETRYABLE = "retryable"  # safe to retry: request not accepted (429, 5xx before body, connect error)
    PERMANENT = "permanent"  # will never succeed (invalid recipient, window closed, opted out)
    UNKNOWN = "unknown"  # request sent, outcome unknown (timeout after send) → POC_03 §7.4


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    provider_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_after_s: float | None = None
    retry_after_unknown_allowed: bool = True  # False for providers where a retry can duplicate (Telegram)


@dataclass
class NewDomainEvent:
    business_id: UUID
    type: str
    source: str
    payload: dict[str, Any]
    correlation_id: UUID
    causation_id: UUID | None = None
    causation_depth: int = 0
    customer_id: UUID | None = None
    conversation_id: UUID | None = None
    occurred_at: datetime | None = None


class Superseded(Exception):
    """A newer customer message arrived; this turn must not send anything."""


class LeaseLost(Exception):
    """Another worker owns this row now (our lease expired)."""


# ---------------------------------------------------------------- turn handle (implemented by pipeline)


@dataclass(frozen=True)
class TurnRef:
    turn_id: UUID
    business_id: UUID
    conversation_id: UUID
    desk: Desk
    priority: PriorityClass
    message_ids: list[UUID]
    inherited_turn_ids: list[UUID]
    attempts: int
    hints: dict[str, Any] = field(default_factory=dict)
    trace_context: dict[str, Any] | None = None  # trace of the first inbound message (one trace end to end)


OutboundWriter = Callable[[AsyncSession], Awaitable[None]]


class TurnHandle(Protocol):
    ref: TurnRef

    async def is_superseded(self) -> bool: ...

    async def checkpoint(self, stage: str) -> None:
        """Raise `Superseded` if superseded; renew the lease (raise `LeaseLost` if it was taken)."""
        ...

    async def commit_reply(self, write_outbound: OutboundWriter, outcome: TurnOutcome) -> None:
        """Atomically: verify not superseded, write outbound rows via `write_outbound(session)`, complete the turn."""
        ...

    async def finish(self, outcome: TurnOutcome) -> None:
        """Complete the turn without a reply (e.g. no_reply, routed elsewhere)."""
        ...


# ---------------------------------------------------------------- ports


class InboundProcessorPort(Protocol):
    async def process_inbox_event(self, event: InboxEvent) -> None: ...


class TurnRunnerPort(Protocol):
    async def run_turn(self, handle: TurnHandle) -> None: ...

    async def send_holding_reply(self, business_id: UUID, conversation_id: UUID, turn_id: UUID) -> None: ...

    async def run_fallback(self, ref: TurnRef, reason: str) -> None:
        """Called once a turn failed permanently: send the holding reply and alert the owner."""
        ...


class OutboundSenderPort(Protocol):
    async def send(self, row: OutboundRow) -> SendResult: ...

    async def on_final_failure(self, row: OutboundRow, result: SendResult) -> None: ...


class NotificationDispatcherPort(Protocol):
    async def dispatch(self, notification: dict[str, Any]) -> None: ...


JobHandler = Callable[[JobRow], Awaitable[None]]


class JobHandlerRegistry(Protocol):
    def register(self, queue: str, handler: JobHandler) -> None: ...

    def get(self, queue: str) -> JobHandler | None: ...


class JobEnqueuer(Protocol):
    async def enqueue(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        business_id: UUID | None = None,
        priority: PriorityClass = PriorityClass.P3,
        dedupe_key: str | None = None,
        delay_s: float = 0,
        max_attempts: int = 5,
        session: AsyncSession | None = None,
    ) -> UUID | None: ...


class DomainEventWriter(Protocol):
    async def append(self, session: AsyncSession, event: NewDomainEvent) -> UUID | None: ...


class TurnScheduler(Protocol):
    async def schedule(
        self,
        session: AsyncSession,
        *,
        business_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        desk: Desk,
        priority: PriorityClass,
        ends_thought: bool,
        hints: dict[str, Any] | None = None,
    ) -> UUID: ...


# ---------------------------------------------------------------- gateway-facing ports (POC_03 §3.2)


@dataclass(frozen=True)
class RoutableEvent:
    """An inbound customer event offered to the automation router before a turn is scheduled."""

    type: str  # message.received | comment.received
    business_id: UUID
    customer_id: UUID
    conversation_id: UUID
    message_id: UUID
    channel: str
    text: str | None
    interactive_option_id: str | None
    domain_event_id: UUID | None
    post_id: str | None = None
    comment_id: str | None = None


class CallRouterPort(Protocol):
    """Provided by voice: deliver a chat message into an active call ("delivered"/"buffered") or "none"."""

    async def deliver_if_active(
        self,
        business_id: UUID,
        customer_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        kind: str = "text",  # text | image | document | audio | location
    ) -> str: ...


class OwnerCommandPort(Protocol):
    """Provided by owner: deterministic owner replies (YES 1234, /undo, buttons). True if handled."""

    async def try_handle(
        self,
        business_id: UUID,
        member_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        text: str | None,
        option_id: str | None,
        reply_to_external_id: str | None = None,
    ) -> bool: ...


@dataclass
class AnswerResult:
    """What happened to an owner's answer (agent's `answer_question`)."""

    question_code: str
    scope: str  # global | customer | temporary
    qa_entry_id: UUID | None
    relayed: bool  # a relay turn was scheduled for the customer
    scope_uncertain: bool = False  # saved for this customer only; the owner is asked whether it applies to all
    already_answered: bool = False


class ShopProceduresPort(Protocol):
    """Provided by automations (ADR-015 awareness): the owner's active procedures in plain words, and what a running
    one is waiting for from this customer — so the agent never contradicts the shop."""

    async def describe(self, business_id: UUID) -> list[str]: ...

    async def waiting_on(self, business_id: UUID, customer_id: UUID | None) -> list[str]: ...


class ProposalPort(Protocol):
    """Provided by agent (ADR-015): carry out the owner's decision on an action the agent proposed — exactly the
    stored action, no new LLM decision — tell the customer, and offer to learn a rule. Returns the owner-facing
    reply."""

    async def decided(self, business_id: UUID, approval_id: UUID, approve: bool, member_id: UUID | None) -> str: ...

    async def learn(self, business_id: UUID, approval_id: UUID, scope: str, member_id: UUID | None) -> str:
        """The owner's answer to "do this myself next time?": g = for everyone, c = this customer, o = just once."""
        ...


class QuestionAnswerPort(Protocol):
    """Provided by agent: the ask-the-owner loop's answer side (POC_04 §7 steps 4-7)."""

    async def answer(
        self,
        business_id: UUID,
        question_id: UUID,
        answer: str,
        *,
        member_id: UUID | None,
        source_message_id: UUID | None = None,
        scope: str | None = None,
    ) -> AnswerResult: ...

    async def set_scope(self, business_id: UUID, question_id: UUID, scope: str) -> AnswerResult: ...


class EventRouterPort(Protocol):
    """Provided by automations: returns "consumed" | "observed" | "none"."""

    async def route_inbound(self, event: RoutableEvent) -> str: ...


class BlockHydrator(Protocol):
    """Provided by knowledge/commerce: fills product/payment/order blocks from database rows at render time."""

    async def product(self, business_id: UUID, offering_id: UUID, variant_id: UUID | None) -> Any: ...

    async def payment_link(self, business_id: UUID, payment_link_id: UUID) -> Any: ...

    async def order_summary(self, business_id: UUID, order_id: UUID) -> Any: ...


class OwnerNotifyPort(Protocol):
    """Provided by owner: route a notification to the owner/team on their channels."""

    async def notify(
        self,
        business_id: UUID,
        kind: str,
        text: str,
        *,
        dedupe_key: str,
        priority: PriorityClass = PriorityClass.P1,
        member_id: UUID | None = None,
        buttons: list[tuple[str, str]] | None = None,
        ref: dict[str, str] | None = None,
        about_conversation: UUID | None = None,
    ) -> None:
        """`about_conversation`: the customer conversation this is about. If it is simulated (Lab, chaos), the
        notification goes only to the simulated owner device — never to the real owner's phone."""
        ...


class EmbedderPort(Protocol):
    """Provided by agent's model gateway (WP-05): text embeddings (768 dims) for search."""

    model: str

    async def embed(self, texts: list[str], *, task: str) -> list[list[float]]: ...  # task: "query" | "document"


class CommerceViewsPort(Protocol):
    """Provided by commerce: views for payment-link and order-summary blocks (BlockHydrator delegates here)."""

    async def payment_link(self, business_id: UUID, payment_link_id: UUID) -> Any: ...

    async def order_summary(self, business_id: UUID, order_id: UUID) -> Any: ...


class MediaUrlPort(Protocol):
    """Provided by gateway: short-lived signed public URL for a stored media object."""

    def url_for(self, media_object_id: UUID) -> str: ...


class ModelGatewayPort(Protocol):
    """Provided by agent (model gateway, POC_04 §2.2): hedged, accounted model calls for other modules."""

    async def chat(
        self,
        role: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        ctx: CallCtx,
        tool_choice: str | None = None,
    ) -> ChatResult: ...

    async def structured(self, role: str, messages: list[ChatMessage], schema: type[_M], *, ctx: CallCtx) -> _M: ...

    async def vision(
        self, role: str, prompt: str, media: list[MediaInput], schema: type[_M], *, ctx: CallCtx
    ) -> _M: ...

    async def embed(self, texts: list[str], *, task: str, ctx: CallCtx | None = None) -> list[list[float]]: ...


class JobQueuePort(Protocol):
    """Provided by pipeline: claim/complete/fail jobs (a dedicated process, e.g. the voice-bridge, owns queues)."""

    async def claim(
        self,
        worker: str,
        lease_s: int,
        limit: int,
        *,
        only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[JobRow]: ...

    async def complete(self, job: JobRow, worker: str) -> None: ...

    async def fail(self, job: JobRow, worker: str, error: str) -> str: ...


class ConversationSenderPort(Protocol):
    """Provided by gateway: send blocks into a conversation outside a turn (order confirmations, owner pings).
    Goes through the normal outbound path (policy, render, idempotency). Returns the message id or None."""

    async def send_blocks(
        self,
        business_id: UUID,
        conversation_id: UUID,
        blocks: list[dict[str, Any]],
        *,
        origin: str,
        idempotency_base: str,
    ) -> UUID | None: ...


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str | None = None
    alternatives: list[str] = field(default_factory=list)
    code: str | None = None


class OrderGuardPort(Protocol):
    """Provided by automations (POC_06 §6): capacity and similar rules checked (and reserved) at order confirm."""

    async def check(self, business_id: UUID, order: dict[str, Any]) -> GuardResult: ...

    async def release(self, business_id: UUID, order_id: UUID, reason: str) -> None: ...


class WorkflowPort(Protocol):
    """Provided by workflows (Temporal client): start durable workflows and deliver signals (POC_01 §5.5)."""

    async def start_import(
        self,
        business_id: UUID,
        owner_file_id: UUID,
        *,
        mode: str,
        member_id: UUID | None,
        reply_conversation_id: UUID,
    ) -> UUID:
        """Start `ImportWorkflow`; returns the import run id (workflow id `import-<run id>`)."""
        ...

    async def start_onboarding_preview(self, business_id: UUID, reply_conversation_id: UUID) -> str:
        """Start `OnboardingPreviewWorkflow` (5 sample customer Q&As for the owner); returns the workflow id."""
        ...

    async def start_automation_build(
        self, business_id: UUID, *, member_id: UUID | None, description: str, reply_conversation_id: UUID, language: str
    ) -> str:
        """Start `AutomationBuildWorkflow` (owner's words → definitions → read-back → YES); returns the workflow id."""
        ...

    async def start_automation_run(
        self,
        business_id: UUID,
        *,
        run_id: UUID,
        version_id: UUID,
        instance_key: str,
        variables: dict[str, Any],
        time_scale: float,
    ) -> bool:
        """Start `AutomationRunWorkflow` (id `auto-<version>-<instance key>`); False if one is already running."""
        ...

    async def cancel(self, workflow_id: str) -> bool:
        """Request cancellation; False if the workflow is already gone."""
        ...

    async def signal(self, workflow_id: str, name: str, payload: dict[str, Any]) -> bool:
        """Deliver a signal; False if the workflow is gone (finished/timed out)."""
        ...
