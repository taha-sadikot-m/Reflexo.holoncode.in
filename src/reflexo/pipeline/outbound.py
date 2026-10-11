"""Outbound send queue mechanics (POC_03 §7): strict per-conversation ordering, idempotency via the send log,
rate limiting per connection, kill switches, and unknown-outcome handling (wait for a status webhook, then
retry at most once)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import OutboundRow, SendResult, SendStatus
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.logging import get_logger
from reflexo.pipeline.jobs import backoff_s

log = get_logger(__name__)

MAX_SEND_ATTEMPTS = 5
MAX_UNKNOWN_ATTEMPTS = 2  # first send + one retry after an unknown outcome

_COLUMNS = (
    "id, business_id, message_id, conversation_id, channel_connection_id, part_index, idempotency_key, "
    "rendered_payload, attempts, status, provider_message_id, trace_context"
)


def _row(r: Any) -> OutboundRow:
    return OutboundRow(
        r.id,
        r.business_id,
        r.message_id,
        r.conversation_id,
        r.channel_connection_id,
        r.part_index,
        r.idempotency_key,
        r.rendered_payload,
        r.attempts,
        str(r.status),
        r.provider_message_id,
        r.trace_context,
    )


async def claim(worker: str, lease_s: int, limit: int) -> list[OutboundRow]:
    """Claim the oldest sendable part of each conversation. Parts behind an unresolved earlier part wait."""
    if limit <= 0:
        return []
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        rows = (
            await s.execute(
                text(f"""
            UPDATE outbound_messages o
            SET status = 'sending', lease_owner = :w, lease_until = now() + make_interval(secs => :lease),
                attempts = attempts + 1
            WHERE o.id IN (
              SELECT c.id FROM outbound_messages c
              WHERE (c.status = 'queued' OR (c.status = 'unconfirmed' AND c.attempts < :max_unknown))
                AND c.next_attempt_at <= now()
                AND NOT EXISTS (
                  SELECT 1 FROM outbound_messages e
                  WHERE e.conversation_id = c.conversation_id AND e.id <> c.id
                    AND (e.status IN ('queued', 'sending') OR (e.status = 'unconfirmed' AND e.attempts < :max_unknown))
                    AND (e.created_at, e.part_index) < (c.created_at, c.part_index))
              ORDER BY c.priority, c.created_at, c.part_index
              LIMIT :n FOR UPDATE SKIP LOCKED)
            RETURNING {_COLUMNS}"""),  # noqa: S608 - constant column list; values are bound parameters
                {"w": worker, "lease": lease_s, "n": limit, "max_unknown": MAX_UNKNOWN_ATTEMPTS},
            )
        ).all()
    return [_row(r) for r in rows]


async def defer(row: OutboundRow, worker: str, delay_s: float) -> None:
    """Rate limited: put back without consuming an attempt."""
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        await s.execute(
            text("""
            UPDATE outbound_messages SET status = CAST(:st AS outbound_status), attempts = attempts - 1,
                   lease_owner = NULL, next_attempt_at = now() + make_interval(secs => :d)
            WHERE id = :id AND lease_owner = :w"""),
            {"st": "unconfirmed" if row.status == "unconfirmed" else "queued", "d": delay_s, "id": row.id, "w": worker},
        )


async def block(row: OutboundRow, worker: str, code: str) -> None:
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        await s.execute(
            text("""UPDATE outbound_messages SET status = 'blocked', lease_owner = NULL, last_error = :e
                                WHERE id = :id AND lease_owner = :w"""),
            {"e": code, "id": row.id, "w": worker},
        )
        await s.execute(
            text("""UPDATE messages SET status = 'blocked', error = jsonb_build_object('code', CAST(:e AS text))
                                WHERE id = :m"""),
            {"e": code, "m": row.message_id},
        )


async def apply_result(row: OutboundRow, worker: str, result: SendResult) -> str:
    """Persist a send result. Returns the resulting outbound status."""
    s_cfg = get_settings()
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        if result.status is SendStatus.SENT:
            await s.execute(
                text("""
                UPDATE outbound_messages SET status = 'sent', provider_message_id = :pid, sent_at = now(),
                       lease_owner = NULL, last_error = NULL WHERE id = :id AND lease_owner = :w"""),
                {"pid": result.provider_message_id, "id": row.id, "w": worker},
            )
            await s.execute(
                text("""
                UPDATE messages SET status = 'sent', external_message_id = COALESCE(external_message_id, :pid)
                WHERE id = :m AND status IN ('queued', 'sending', 'unconfirmed')"""),
                {"pid": result.provider_message_id, "m": row.message_id},
            )
            return "sent"

        if result.status is SendStatus.UNKNOWN:
            # One more attempt is allowed only if the provider tolerates it; otherwise final 'unconfirmed'.
            attempts_floor = row.attempts if result.retry_after_unknown_allowed else MAX_UNKNOWN_ATTEMPTS
            await s.execute(
                text("""
                UPDATE outbound_messages SET status = 'unconfirmed', lease_owner = NULL, last_error = :e,
                       attempts = GREATEST(attempts, :floor),
                       next_attempt_at = now() + make_interval(secs => :wait)
                WHERE id = :id AND lease_owner = :w"""),
                {
                    "e": result.error_message,
                    "floor": attempts_floor,
                    "wait": s_cfg.outbound_unknown_wait_s,
                    "id": row.id,
                    "w": worker,
                },
            )
            await s.execute(
                text("UPDATE messages SET status = 'unconfirmed' WHERE id = :m AND status <> 'sent'"),
                {"m": row.message_id},
            )
            log.warning("outbound_unknown_outcome", outbound_id=str(row.id), attempts=row.attempts)
            return "unconfirmed"

        final = result.status is SendStatus.PERMANENT or row.attempts >= MAX_SEND_ATTEMPTS
        if not final:
            delay = max(backoff_s(row.attempts), result.retry_after_s or 0)
            await s.execute(
                text("""
                UPDATE outbound_messages SET status = 'queued', lease_owner = NULL, last_error = :e,
                       next_attempt_at = now() + make_interval(secs => :d) WHERE id = :id AND lease_owner = :w"""),
                {"e": f"{result.error_code}: {result.error_message}", "d": delay, "id": row.id, "w": worker},
            )
            return "queued"
        await s.execute(
            text("""
            UPDATE outbound_messages SET status = 'failed', lease_owner = NULL, last_error = :e
            WHERE id = :id AND lease_owner = :w"""),
            {"e": f"{result.error_code}: {result.error_message}", "id": row.id, "w": worker},
        )
        await s.execute(
            text("""
            UPDATE messages SET status = 'failed',
                   error = jsonb_build_object('code', CAST(:c AS text), 'message', CAST(:msg AS text))
            WHERE id = :m"""),
            {"c": result.error_code, "msg": result.error_message, "m": row.message_id},
        )
        return "failed"


async def confirm_by_provider_id(provider_message_id: str, delivered_status: str) -> bool:
    """Called by the gateway status handler: resolves an `unconfirmed` part when the provider reports it."""
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        updated = (
            await s.execute(
                text("""
            UPDATE outbound_messages SET status = 'sent', sent_at = COALESCE(sent_at, now())
            WHERE provider_message_id = :pid AND status IN ('unconfirmed', 'sending')
            RETURNING message_id"""),
                {"pid": provider_message_id},
            )
        ).scalar_one_or_none()
        if updated is None:
            return False
        await s.execute(
            text("UPDATE messages SET status = CAST(:st AS message_status) WHERE id = :m"),
            {"st": delivered_status, "m": updated},
        )
        return True


async def sweep_expired() -> int:
    """A worker died while sending: the outcome is unknown, so treat it like a send timeout."""
    async with get_db().service_session(ServiceReason.QUEUE_CLAIM) as s:
        result = (
            await s.execute(
                text("""
            UPDATE outbound_messages SET status = 'unconfirmed', lease_owner = NULL,
                   next_attempt_at = now() + make_interval(secs => :wait), last_error = 'worker lost during send'
            WHERE status = 'sending' AND lease_until < now()
            RETURNING id"""),
                {"wait": get_settings().outbound_unknown_wait_s},
            )
        ).all()
    return len(result)
