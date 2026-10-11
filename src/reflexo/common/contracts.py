"""Cross-module contracts: port protocols and the value types they exchange (POC_01 §4.2, POC_03).

Ports are provided by the owning module at bootstrap and required by consumers, so the pipeline can drive
gateway/agent/owner code without importing those modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.enums import Desk, PriorityClass, TurnOutcome

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
