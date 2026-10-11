"""Turn scheduling, coalescing, supersession and crash recovery (POC_03 §4; specs_v2 D-021/D-022).

Invariants (enforced by partial unique indexes):
* at most one `pending` and one `running` turn per conversation;
* a running turn that is superseded never sends: `commit_reply` locks the turn row and re-checks inside the
  same transaction that writes the outbound rows; `schedule` sets `superseded_by` under the same row lock,
  so "committed" and "superseded" are mutually exclusive.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.config import get_settings
from reflexo.common.contracts import LeaseLost, OutboundWriter, Superseded, TurnRef
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import Desk, PriorityClass, TurnOutcome
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.tracing import current_trace_context
from reflexo.pipeline.jobs import Jobs, backoff_s

log = get_logger(__name__)

HOLDING_QUEUE = "turn.holding_check"


def holding_key(turn_id: UUID) -> str:
    return f"turn:{turn_id}:holding"


_TURN_COLUMNS = "id, business_id, conversation_id, desk, priority, message_ids, inherited_turn_ids, attempts, hints"


def _ref(row: Any) -> TurnRef:
    return TurnRef(
        turn_id=row.id,
        business_id=row.business_id,
        conversation_id=row.conversation_id,
        desk=Desk(row.desk),
        priority=PriorityClass(row.priority),
        message_ids=list(row.message_ids),
        inherited_turn_ids=list(row.inherited_turn_ids),
        attempts=row.attempts,
        hints=dict(row.hints or {}),
    )


class TurnService:
    """Implements `TurnScheduler` plus the worker-side operations on turns."""

    def __init__(self, jobs: Jobs) -> None:
        self._jobs = jobs

    # ------------------------------------------------------------------ scheduling (producer side)
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
    ) -> UUID:
        s = get_settings()
        window_s = (s.coalesce_short_ms if ends_thought else s.coalesce_window_ms) / 1000
        pending_id: UUID = (
            await session.execute(
                text("""
            INSERT INTO conversation_turns (id, business_id, conversation_id, desk, priority, status, message_ids,
                                            first_message_at, run_after, hints, trace_context)
            VALUES (:id, :b, :c, CAST(:desk AS desk), CAST(:prio AS priority_class), 'pending', ARRAY[CAST(:m AS uuid)],
                    now(), now() + make_interval(secs => :win), CAST(:hints AS jsonb), CAST(:trace AS jsonb))
            ON CONFLICT (conversation_id) WHERE status = 'pending'
            DO UPDATE SET
              message_ids = CASE WHEN CAST(:m AS uuid) = ANY(conversation_turns.message_ids)
                                 THEN conversation_turns.message_ids
                                 ELSE array_append(conversation_turns.message_ids, CAST(:m AS uuid)) END,
              run_after = LEAST(now() + make_interval(secs => :win),
                                conversation_turns.first_message_at + make_interval(secs => :cap)),
              priority = LEAST(conversation_turns.priority, EXCLUDED.priority),
              hints = conversation_turns.hints || EXCLUDED.hints
            RETURNING id"""),
                {
                    "id": new_id(),
                    "b": business_id,
                    "c": conversation_id,
                    "desk": str(desk),
                    "prio": str(priority),
                    "m": message_id,
                    "win": window_s,
                    "cap": s.coalesce_cap_ms / 1000,
                    "hints": json.dumps(hints or {}),
                    "trace": json.dumps(current_trace_context()),
                },
            )
        ).scalar_one()

        # Supersede a running turn (first time only) and carry its unanswered messages into the pending turn.
        running = (
            await session.execute(
                text("""
            UPDATE conversation_turns SET superseded_by = :p
            WHERE conversation_id = :c AND status = 'running' AND superseded_by IS NULL
            RETURNING id, message_ids"""),
                {"p": pending_id, "c": conversation_id},
            )
        ).first()
        if running is not None:
            await session.execute(
                text("""
                UPDATE conversation_turns
                SET message_ids = ARRAY(SELECT DISTINCT unnest(message_ids || CAST(:old AS uuid[]))),
                    inherited_turn_ids = array_append(inherited_turn_ids, CAST(:rid AS uuid))
                WHERE id = :p"""),
                {"old": list(running.message_ids), "rid": running.id, "p": pending_id},
            )
            log.info("turn_superseded", running_turn=str(running.id), pending_turn=str(pending_id))
        await session.execute(text("SELECT pg_notify('q_turns', '')"))
        return pending_id

    # ------------------------------------------------------------------ claiming (worker side)
    async def claim_due(self, worker: str, limit: int) -> list[TurnRef]:
        if limit <= 0:
            return []
        s = get_settings()
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as session:
            rows = (
                await session.execute(
                    text(f"""
                UPDATE conversation_turns t
                SET status = 'running', started_at = now(), lease_owner = :w,
                    lease_until = now() + make_interval(secs => :lease), attempts = attempts + 1
                WHERE t.id IN (
                  SELECT p.id FROM conversation_turns p
                  WHERE p.status = 'pending' AND p.run_after <= now()
                    AND NOT EXISTS (SELECT 1 FROM conversation_turns r
                                    WHERE r.conversation_id = p.conversation_id AND r.status = 'running')
                    AND (SELECT count(*) FROM conversation_turns r2
                         WHERE r2.business_id = p.business_id AND r2.status = 'running') < :cap
                  ORDER BY p.priority, p.run_after
                  LIMIT :n FOR UPDATE SKIP LOCKED)
                RETURNING {_TURN_COLUMNS}"""),  # noqa: S608 - constant column list; values are bound parameters
                    {"w": worker, "lease": s.turn_lease_s, "cap": s.turn_business_concurrency, "n": limit},
                )
            ).all()
            refs = [_ref(r) for r in rows]
            for ref in refs:
                await session.execute(
                    text(
                        "UPDATE messages SET turn_id = :t "
                        "WHERE id = ANY(:m) AND direction = 'inbound' AND turn_id IS NULL"
                    ),
                    {"t": ref.turn_id, "m": ref.message_ids},
                )
                await self._jobs.enqueue(
                    HOLDING_QUEUE,
                    {"turn_id": str(ref.turn_id)},
                    business_id=ref.business_id,
                    priority=PriorityClass.P0,
                    dedupe_key=holding_key(ref.turn_id),
                    delay_s=s.holding_after_s,
                    session=session,
                )
        return refs

    async def requeue_or_merge(self, session: AsyncSession, turn_id: UUID, *, delay_s: float = 0) -> str:
        """Return a running turn to `pending`, or merge it into an existing pending turn (which then inherits it)."""
        row = (
            await session.execute(
                text("SELECT conversation_id, message_ids FROM conversation_turns WHERE id = :id FOR UPDATE"),
                {"id": turn_id},
            )
        ).one()
        pending = (
            await session.execute(
                text("SELECT id FROM conversation_turns WHERE conversation_id = :c AND status = 'pending' FOR UPDATE"),
                {"c": row.conversation_id},
            )
        ).scalar_one_or_none()
        if pending is None:
            await session.execute(
                text("""
                UPDATE conversation_turns SET status = 'pending', lease_owner = NULL, lease_until = NULL,
                       run_after = now() + make_interval(secs => :d) WHERE id = :id"""),
                {"id": turn_id, "d": delay_s},
            )
            await session.execute(text("SELECT pg_notify('q_turns', '')"))
            return "requeued"
        await session.execute(
            text("""
            UPDATE conversation_turns
            SET message_ids = ARRAY(SELECT DISTINCT unnest(message_ids || CAST(:old AS uuid[]))),
                inherited_turn_ids = array_append(inherited_turn_ids, CAST(:rid AS uuid))
            WHERE id = :p"""),
            {"old": list(row.message_ids), "rid": turn_id, "p": pending},
        )
        await session.execute(
            text("""
            UPDATE conversation_turns SET status = 'superseded', superseded_by = :p, finished_at = now(),
                   lease_owner = NULL WHERE id = :id"""),
            {"p": pending, "id": turn_id},
        )
        return "merged"

    async def release_for_retry(self, ref: TurnRef, worker: str) -> str:
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as session:
            owned = (
                await session.execute(
                    text("SELECT 1 FROM conversation_turns WHERE id = :id AND lease_owner = :w AND status = 'running'"),
                    {"id": ref.turn_id, "w": worker},
                )
            ).scalar_one_or_none()
            if owned is None:
                return "lease_lost"
            return await self.requeue_or_merge(session, ref.turn_id, delay_s=backoff_s(ref.attempts))

    async def mark(self, ref: TurnRef, status: str, outcome: TurnOutcome | None = None) -> None:
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as session:
            await session.execute(
                text("""
                UPDATE conversation_turns SET status = CAST(:st AS turn_status),
                       outcome = COALESCE(CAST(:o AS turn_outcome), outcome), finished_at = now(), lease_owner = NULL
                WHERE id = :id"""),
                {"st": status, "o": str(outcome) if outcome else None, "id": ref.turn_id},
            )
            await self._jobs.cancel(HOLDING_QUEUE, holding_key(ref.turn_id), session=session)

    async def sweep_expired(self) -> list[TurnRef]:
        """Recover turns whose worker died (lease expired). Returns turns that exhausted their attempts."""
        exhausted: list[TurnRef] = []
        max_attempts = get_settings().turn_max_attempts
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as session:
            rows = (
                await session.execute(
                    text(f"""
                SELECT {_TURN_COLUMNS} FROM conversation_turns
                WHERE status = 'running' AND lease_until < now() FOR UPDATE SKIP LOCKED""")  # noqa: S608 - constant column list; values are bound parameters
                )
            ).all()
            for r in rows:
                ref = _ref(r)
                if ref.attempts >= max_attempts:
                    await session.execute(
                        text("""UPDATE conversation_turns SET status = 'failed', outcome = 'fallback',
                                                  finished_at = now(), lease_owner = NULL WHERE id = :id"""),
                        {"id": ref.turn_id},
                    )
                    exhausted.append(ref)
                else:
                    result = await self.requeue_or_merge(session, ref.turn_id)
                    log.warning("turn_lease_expired", turn_id=str(ref.turn_id), result=result, attempts=ref.attempts)
        return exhausted


class PipelineTurnHandle:
    """Implements `TurnHandle` for one claimed turn."""

    SUPERSEDE_CACHE_S = 0.2

    def __init__(self, ref: TurnRef, worker: str, service: TurnService) -> None:
        self.ref = ref
        self._worker = worker
        self._service = service
        self._checked_at = 0.0
        self._superseded = False

    async def is_superseded(self) -> bool:
        now = time.monotonic()
        if self._superseded or now - self._checked_at < self.SUPERSEDE_CACHE_S:
            return self._superseded
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            sup = (
                await s.execute(
                    text("SELECT superseded_by FROM conversation_turns WHERE id = :id"), {"id": self.ref.turn_id}
                )
            ).scalar_one_or_none()
        self._checked_at = now
        self._superseded = sup is not None
        return self._superseded

    async def checkpoint(self, stage: str) -> None:
        async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
            row = (
                await s.execute(
                    text("""
                UPDATE conversation_turns SET lease_until = now() + make_interval(secs => :lease)
                WHERE id = :id AND lease_owner = :w AND status = 'running'
                RETURNING superseded_by"""),
                    {"lease": get_settings().turn_lease_s, "id": self.ref.turn_id, "w": self._worker},
                )
            ).first()
        if row is None:
            raise LeaseLost(f"turn {self.ref.turn_id} at {stage}")
        if row.superseded_by is not None:
            self._superseded = True
            raise Superseded(f"turn {self.ref.turn_id} at {stage}")

    async def commit_reply(self, write_outbound: OutboundWriter, outcome: TurnOutcome) -> None:
        async with get_db().tenant_session(self.ref.business_id) as s:
            row = (
                await s.execute(
                    text("""
                SELECT status, lease_owner, superseded_by FROM conversation_turns WHERE id = :id FOR UPDATE"""),
                    {"id": self.ref.turn_id},
                )
            ).one()
            if row.status != "running" or row.lease_owner != self._worker:
                raise LeaseLost(f"turn {self.ref.turn_id} at commit")
            if row.superseded_by is not None:
                self._superseded = True
                raise Superseded(f"turn {self.ref.turn_id} at commit")
            await write_outbound(s)
            await s.execute(
                text("""
                UPDATE conversation_turns SET status = 'completed', outcome = CAST(:o AS turn_outcome),
                       finished_at = now(), lease_owner = NULL WHERE id = :id"""),
                {"o": str(outcome), "id": self.ref.turn_id},
            )
            await s.execute(
                text("""UPDATE jobs SET status = 'done'
                                    WHERE queue = :q AND dedupe_key = :k AND status = 'pending'"""),
                {"q": HOLDING_QUEUE, "k": holding_key(self.ref.turn_id)},
            )
            await s.execute(text("SELECT pg_notify('q_outbound', '')"))

    async def finish(self, outcome: TurnOutcome) -> None:
        status = "superseded" if await self.is_superseded() else "completed"
        await self._service.mark(self.ref, status, outcome)
