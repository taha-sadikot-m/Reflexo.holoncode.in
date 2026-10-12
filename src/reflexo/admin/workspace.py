"""Admin workspace pages (POC_07 §2, WP-13a): Orders, Automations, Playbook, Owner, Catalogue history, Imports, Calls.

Read models are plain SQL under the business's RLS context (CHK-F12-2). Mutations go through the owning module's
own path: undo is a change-set revert (so a rule removed here is removed exactly like `/undo` on the phone),
skip-wait is the workflow's `skip_wait` signal, pause/resume flips the automation's status (only `active`
automations are routed — `automations.active_versions`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.knowledge.api as knowledge
from reflexo.common import flags, ports
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import get_db
from reflexo.common.logging import mask_phone_numbers

ChangeConflict = knowledge.ChangeConflict
_ORDER_STATUSES = ("draft", "awaiting_payment", "confirmed", "preparing", "out_for_delivery", "delivered", "cancelled")


def _iso(v: datetime | date | None) -> str | None:
    return v.isoformat() if v else None


def _rupees(paise: int | None) -> float | None:
    return None if paise is None else paise / 100


def _who(name: str | None, phone: str | None) -> str | None:
    return name or (mask_phone_numbers(phone) if phone else None)


# ---------------------------------------------------------------- orders


async def orders(business_id: UUID, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    if status is not None and status not in _ORDER_STATUSES:
        raise ValueError(f"unknown status {status}")
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT o.id, o.order_number, o.status, o.payment_method, o.payment_status, o.total_paise,
                   o.delivery_date, o.delivery_slot, o.source_channel, o.created_at, o.cancelled_by,
                   cu.display_name, cu.phone_e164, c.simulated,
                   (SELECT string_agg(i.quantity || '× ' || i.name_snapshot, ', ' ORDER BY i.name_snapshot)
                      FROM order_items i WHERE i.order_id = o.id) AS items
            FROM orders o
            LEFT JOIN customers cu ON cu.id = o.customer_id
            LEFT JOIN conversations c ON c.id = o.conversation_id
            WHERE (CAST(:st AS text) IS NULL OR o.status::text = :st)
            ORDER BY o.created_at DESC LIMIT :n"""),
                {"st": status, "n": limit},
            )
        ).all()
        counts = (await s.execute(text("SELECT status::text, count(*) FROM orders GROUP BY status"))).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "order_number": r.order_number,
                "status": str(r.status),
                "payment_method": str(r.payment_method) if r.payment_method else None,
                "payment_status": str(r.payment_status) if r.payment_status else None,
                "total_rupees": _rupees(r.total_paise),
                "delivery_date": _iso(r.delivery_date),
                "delivery_slot": r.delivery_slot,
                "channel": str(r.source_channel) if r.source_channel else None,
                "customer": _who(r.display_name, r.phone_e164),
                "items": r.items or "",
                "simulated": bool(r.simulated),
                "cancelled_by": r.cancelled_by,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ],
        "counts": {k: int(v) for k, v in counts},
    }


async def order_detail(business_id: UUID, order_id: UUID) -> dict[str, Any] | None:
    async with get_db().tenant_session(business_id) as s:
        o = (
            await s.execute(
                text("""SELECT o.*, cu.display_name, cu.phone_e164 FROM orders o
                        LEFT JOIN customers cu ON cu.id = o.customer_id WHERE o.id = :id"""),
                {"id": order_id},
            )
        ).first()
        if o is None:
            return None
        items = (
            await s.execute(
                text("""SELECT name_snapshot, quantity, unit_price_paise, line_total_paise, details
                        FROM order_items WHERE order_id = :id ORDER BY name_snapshot"""),
                {"id": order_id},
            )
        ).all()
        links = (
            await s.execute(
                text("""SELECT id, amount_paise, status, short_url, created_at, paid_at, expires_at
                        FROM payment_links WHERE order_id = :id ORDER BY created_at"""),
                {"id": order_id},
            )
        ).all()
        events = (
            await s.execute(
                text("""SELECT type, occurred_at, source, payload FROM domain_events
                        WHERE payload->>'order_id' = :id ORDER BY occurred_at"""),
                {"id": str(order_id)},
            )
        ).all()
        proposals = (
            await s.execute(
                text("""SELECT id, status, preview, subject->>'capability' AS capability, decision, created_at,
                               decided_at FROM approval_requests
                        WHERE kind = 'tool_call' AND subject->'args'->>'order_id' = :id ORDER BY created_at"""),
                {"id": str(order_id)},
            )
        ).all()
    return {
        "id": str(o.id),
        "order_number": o.order_number,
        "status": str(o.status),
        "customer": _who(o.display_name, o.phone_e164),
        "conversation_id": str(o.conversation_id) if o.conversation_id else None,
        "payment_method": str(o.payment_method) if o.payment_method else None,
        "payment_status": str(o.payment_status) if o.payment_status else None,
        "subtotal_rupees": _rupees(o.subtotal_paise),
        "delivery_fee_rupees": _rupees(o.delivery_fee_paise),
        "total_rupees": _rupees(o.total_paise),
        "delivery_date": _iso(o.delivery_date),
        "delivery_slot": o.delivery_slot,
        "address": mask_phone_numbers(o.address) if o.address else None,
        "notes": o.notes,
        "details": o.details or {},
        "missing_details": list(o.missing_details or []),
        "created_by": o.created_by,
        "created_at": _iso(o.created_at),
        "confirmed_at": _iso(o.confirmed_at),
        "cancelled": {"at": _iso(o.cancelled_at), "by": o.cancelled_by, "reason": o.cancel_reason}
        if o.cancelled_at
        else None,
        "items": [
            {
                "name": i.name_snapshot,
                "quantity": i.quantity,
                "unit_rupees": _rupees(i.unit_price_paise),
                "line_rupees": _rupees(i.line_total_paise),
                "details": i.details or {},
            }
            for i in items
        ],
        "payment_links": [
            {
                "id": str(p.id),
                "amount_rupees": _rupees(p.amount_paise),
                "status": str(p.status),
                "url": p.short_url,
                "created_at": _iso(p.created_at),
                "paid_at": _iso(p.paid_at),
            }
            for p in links
        ],
        "timeline": [
            {"type": e.type, "at": _iso(e.occurred_at), "source": e.source, "payload": e.payload} for e in events
        ],
        "proposals": [
            {
                "id": str(p.id),
                "capability": p.capability,
                "status": str(p.status),
                "summary": (p.preview or {}).get("summary"),
                "outcome": (p.decision or {}).get("outcome"),
                "created_at": _iso(p.created_at),
                "decided_at": _iso(p.decided_at),
            }
            for p in proposals
        ],
    }


async def capacity(business_id: UUID) -> list[dict[str, Any]]:
    """Daily limits held by automations (`automation_counters`), e.g. `cakes:2026-10-14` → 3 of 8."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT c.counter_key, c.value, c.limit_value, a.name FROM automation_counters c
                        JOIN automations a ON a.id = c.automation_id
                        ORDER BY c.counter_key""")
            )
        ).all()
    return [{"key": r.counter_key, "used": r.value, "limit": r.limit_value, "automation": r.name} for r in rows]


# ---------------------------------------------------------------- automations


async def automations(business_id: UUID) -> list[dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT a.id, a.name, a.description_nl, a.status, a.priority, a.created_at, v.version, v.executor,
                   v.readback, rc.runs_today, rc.live_runs
            FROM automations a LEFT JOIN automation_versions v ON v.id = a.current_version_id
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE r.started_at > now() - interval '24 hours') AS runs_today,
                       count(*) FILTER (WHERE r.status IN ('running', 'waiting')) AS live_runs
                FROM automation_runs r JOIN automation_versions av ON av.id = r.automation_version_id
                WHERE av.automation_id = a.id) rc
            WHERE a.status <> 'archived'
            ORDER BY a.priority, a.created_at""")
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "description": r.description_nl,
            "status": str(r.status),
            "version": r.version,
            "executor": str(r.executor) if r.executor else None,
            "readback": r.readback,
            "runs_today": int(r.runs_today),
            "live_runs": int(r.live_runs),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def automation_detail(business_id: UUID, automation_id: UUID, runs: int = 30) -> dict[str, Any] | None:
    async with get_db().tenant_session(business_id) as s:
        a = (
            await s.execute(
                text("""SELECT a.id, a.name, a.description_nl, a.status, v.version, v.executor, v.readback,
                               v.definition, v.activated_at
                        FROM automations a LEFT JOIN automation_versions v ON v.id = a.current_version_id
                        WHERE a.id = :id"""),
                {"id": automation_id},
            )
        ).first()
        if a is None:
            return None
        run_rows = (
            await s.execute(
                text("""SELECT r.id, r.instance_key, r.status, r.workflow_id, r.started_at, r.finished_at, r.error,
                               av.version
                        FROM automation_runs r JOIN automation_versions av ON av.id = r.automation_version_id
                        WHERE av.automation_id = :id ORDER BY r.started_at DESC LIMIT :n"""),
                {"id": automation_id, "n": runs},
            )
        ).all()
        ids = [r.id for r in run_rows]
        steps = (
            (
                await s.execute(
                    text("""SELECT run_id, step_id, status, output, error, started_at, finished_at
                            FROM automation_run_steps WHERE run_id = ANY(:ids) ORDER BY started_at NULLS LAST"""),
                    {"ids": ids},
                )
            ).all()
            if ids
            else []
        )
        waits = (
            (
                await s.execute(
                    text("""SELECT run_id, event_type, status, expires_at FROM automation_waits
                            WHERE run_id = ANY(:ids) ORDER BY created_at"""),
                    {"ids": ids},
                )
            ).all()
            if ids
            else []
        )
    by_run: dict[UUID, dict[str, list[dict[str, Any]]]] = {i: {"steps": [], "waits": []} for i in ids}
    for st in steps:
        by_run[st.run_id]["steps"].append(
            {
                "step_id": st.step_id,
                "status": str(st.status),
                "output": st.output,
                "error": st.error,
                "started_at": _iso(st.started_at),
                "finished_at": _iso(st.finished_at),
            }
        )
    for w in waits:
        by_run[w.run_id]["waits"].append(
            {"event_type": w.event_type, "status": w.status, "expires_at": _iso(w.expires_at)}
        )
    return {
        "id": str(a.id),
        "name": a.name,
        "description": a.description_nl,
        "status": str(a.status),
        "version": a.version,
        "executor": str(a.executor) if a.executor else None,
        "readback": a.readback,
        "definition": a.definition,
        "activated_at": _iso(a.activated_at),
        "runs": [
            {
                "id": str(r.id),
                "instance_key": r.instance_key,
                "status": str(r.status),
                "workflow_id": r.workflow_id,
                "version": r.version,
                "started_at": _iso(r.started_at),
                "finished_at": _iso(r.finished_at),
                "error": r.error,
                **by_run[r.id],
            }
            for r in run_rows
        ],
    }


async def set_automation_status(business_id: UUID, automation_id: UUID, status: str) -> str:
    """Pause or resume. Only `active` automations receive events, so a paused one stops starting runs; runs
    already waiting continue (pausing is not cancelling)."""
    if status not in ("active", "paused"):
        raise ValueError("status must be active or paused")
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(
                text("""UPDATE automations SET status = CAST(:st AS automation_status), updated_at = now()
                        WHERE id = :id AND status IN ('active', 'paused') AND current_version_id IS NOT NULL
                        RETURNING status"""),
                {"id": automation_id, "st": status},
            )
        ).first()
    if row is None:
        raise LookupError("automation not found or not pausable")
    return str(row.status)


async def skip_wait(business_id: UUID, run_id: UUID) -> bool:
    """Fast-forward a waiting run (POC_06 demo time): the workflow's `skip_wait` signal."""
    async with get_db().tenant_session(business_id) as s:
        wf = (
            await s.execute(
                text("""SELECT workflow_id FROM automation_runs
                        WHERE id = :id AND status IN ('running', 'waiting') AND workflow_id IS NOT NULL"""),
                {"id": run_id},
            )
        ).scalar_one_or_none()
    if wf is None:
        raise LookupError("no live durable run with that id")
    sent: bool = await ports.registry.require(WorkflowPort).signal(wf, "skip_wait", {})  # type: ignore[type-abstract]
    return sent


# ---------------------------------------------------------------- playbook (ADR-015, POC_10)


async def playbook(business_id: UUID) -> dict[str, Any]:
    """Rules with their read-back and where they came from (an owner instruction, or a decision the owner made on
    a proposal), plus the proposals themselves — the learning loop end to end."""
    async with get_db().tenant_session(business_id) as s:
        rule_rows = (
            await s.execute(
                text("""
            SELECT r.id, r.capability, r.effect, r.conditions, r.says, r.scope, r.customer_id, r.source,
                   r.source_ref, r.status, r.created_at, r.updated_at, cu.display_name, cu.phone_e164,
                   ap.preview AS source_preview, ap.decided_at AS source_decided_at,
                   (SELECT i.change_set_id FROM knowledge_change_items i JOIN knowledge_change_sets cs
                          ON cs.id = i.change_set_id
                     WHERE i.entity_type = 'playbook_rule' AND i.entity_id = r.id AND i.op = 'create'
                       AND cs.status = 'applied' LIMIT 1) AS change_set_id
            FROM playbook_rules r
            LEFT JOIN customers cu ON cu.id = r.customer_id
            LEFT JOIN approval_requests ap ON ap.id = r.source_ref
            ORDER BY r.status = 'active' DESC, r.created_at DESC LIMIT 200""")
            )
        ).all()
        proposal_rows = (
            await s.execute(
                text("""
            SELECT a.id, a.status, a.preview, a.subject, a.decision, a.created_at, a.decided_at, a.decided_via,
                   cu.display_name, cu.phone_e164
            FROM approval_requests a
            LEFT JOIN customers cu ON cu.id = CAST(NULLIF(a.subject->>'customer_id', '') AS uuid)
            WHERE a.kind = 'tool_call' ORDER BY a.created_at DESC LIMIT 100""")
            )
        ).all()
    labels = {name: cap.label for name, cap in agent.CAPABILITIES.items()}
    rules = []
    for r in rule_rows:
        rule = knowledge.playbook.PlaybookRule(
            id=r.id,
            capability=r.capability,
            effect=r.effect,
            conditions=[knowledge.playbook.Condition.model_validate(c) for c in r.conditions or []],
            says=r.says,
            source=r.source,
        )
        rules.append(
            {
                "id": str(r.id),
                "capability": r.capability,
                "effect": r.effect,
                "readback": rule.readback(labels.get(r.capability, r.capability)),
                "says": r.says,
                "scope": str(r.scope),
                "customer": _who(r.display_name, r.phone_e164) if r.customer_id else None,
                "source": r.source,
                "source_decision": {
                    "approval_id": str(r.source_ref),
                    "summary": (r.source_preview or {}).get("summary"),
                    "decided_at": _iso(r.source_decided_at),
                }
                if r.source_preview is not None
                else None,
                "status": str(r.status),
                "change_set_id": str(r.change_set_id) if r.change_set_id else None,
                "created_at": _iso(r.created_at),
                "updated_at": _iso(r.updated_at),
            }
        )
    proposals = []
    for p in proposal_rows:
        sub, outcome = p.subject or {}, (p.decision or {}).get("outcome") or {}
        proposals.append(
            {
                "id": str(p.id),
                "capability": sub.get("capability"),
                "label": labels.get(str(sub.get("capability")), sub.get("capability")),
                "summary": (p.preview or {}).get("summary"),
                "reason": sub.get("reason"),
                "customer": _who(p.display_name, p.phone_e164),
                "owner_initiated": bool(sub.get("owner_initiated")),
                "status": str(p.status),
                "done": outcome.get("done"),
                "learned": outcome.get("learned"),
                "conversation_id": sub.get("conversation_id"),
                "created_at": _iso(p.created_at),
                "decided_at": _iso(p.decided_at),
                "decided_via": str(p.decided_via) if p.decided_via else None,
            }
        )
    return {"rules": rules, "proposals": proposals}


# ---------------------------------------------------------------- owner workspace


async def owner(business_id: UUID, limit: int = 50) -> dict[str, Any]:
    async with get_db().tenant_session(business_id) as s:
        questions = (
            await s.execute(
                text("""SELECT q.id, q.question, q.ai_guess, q.status, q.short_code, q.answer, q.answer_scope,
                               q.asked_at, q.answered_at, q.relayed_at, q.sla_due_at, q.conversation_id,
                               cu.display_name, cu.phone_e164
                        FROM owner_questions q LEFT JOIN customers cu ON cu.id = q.customer_id
                        ORDER BY q.created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
        approvals = (
            await s.execute(
                text("""SELECT id, kind, status, preview, short_code, requested_by, expires_at, decided_at,
                               decided_via, created_at
                        FROM approval_requests ORDER BY created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
        notes = (
            await s.execute(
                text("""SELECT kind, priority, status, payload, delivered_via, delivered_at, created_at
                        FROM owner_notifications ORDER BY created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
        files = (
            await s.execute(
                text("""SELECT f.id, f.filename, f.kind, f.description, f.received_via, f.created_at, m.mime,
                               m.size_bytes,
                               (SELECT count(*) FROM import_runs i WHERE i.source_file_id = f.id) AS imports
                        FROM owner_files f JOIN media_objects m ON m.id = f.media_object_id
                        ORDER BY f.created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    return {
        "questions": [
            {
                "id": str(q.id),
                "question": q.question,
                "ai_guess": q.ai_guess,
                "status": str(q.status),
                "code": q.short_code.strip() if q.short_code else None,
                "answer": q.answer,
                "scope": str(q.answer_scope) if q.answer_scope else None,
                "customer": _who(q.display_name, q.phone_e164),
                "conversation_id": str(q.conversation_id) if q.conversation_id else None,
                "asked_at": _iso(q.asked_at),
                "answered_at": _iso(q.answered_at),
                "relayed_at": _iso(q.relayed_at),
                "sla_due_at": _iso(q.sla_due_at),
            }
            for q in questions
        ],
        "approvals": [
            {
                "id": str(a.id),
                "kind": str(a.kind),
                "status": str(a.status),
                "summary": (a.preview or {}).get("summary") or (a.preview or {}).get("text"),
                "code": a.short_code.strip() if a.short_code else None,
                "requested_by": a.requested_by,
                "expires_at": _iso(a.expires_at),
                "decided_at": _iso(a.decided_at),
                "decided_via": str(a.decided_via) if a.decided_via else None,
                "created_at": _iso(a.created_at),
            }
            for a in approvals
        ],
        "notifications": [
            {
                "kind": n.kind,
                "priority": str(n.priority),
                "status": str(n.status),
                "text": (n.payload or {}).get("text"),
                "delivered_via": str(n.delivered_via) if n.delivered_via else None,
                "delivered_at": _iso(n.delivered_at),
                "created_at": _iso(n.created_at),
            }
            for n in notes
        ],
        "files": [
            {
                "id": str(f.id),
                "filename": f.filename,
                "kind": str(f.kind),
                "description": f.description,
                "received_via": str(f.received_via) if f.received_via else None,
                "mime": f.mime,
                "size_bytes": f.size_bytes,
                "imports": int(f.imports),
                "created_at": _iso(f.created_at),
            }
            for f in files
        ],
    }


# ---------------------------------------------------------------- catalogue history (change sets + undo)


async def change_sets(business_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT cs.id, cs.origin, cs.summary, cs.status, cs.revert_of, cs.applied_at, cs.reverted_at, cs.created_at,
                   coalesce(json_agg(json_build_object('entity', i.entity_type, 'op', i.op, 'before', i.before,
                                                       'after', i.after) ORDER BY i.id)
                            FILTER (WHERE i.id IS NOT NULL), '[]') AS items
            FROM knowledge_change_sets cs LEFT JOIN knowledge_change_items i ON i.change_set_id = cs.id
            GROUP BY cs.id ORDER BY cs.created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "origin": str(r.origin),
            "summary": r.summary,
            "status": str(r.status),
            "revert_of": str(r.revert_of) if r.revert_of else None,
            "applied_at": _iso(r.applied_at),
            "reverted_at": _iso(r.reverted_at),
            "created_at": _iso(r.created_at),
            "items": r.items,
        }
        for r in rows
    ]


async def revert_change_set(business_id: UUID, change_set_id: UUID) -> dict[str, Any]:
    """The same undo the owner gets with `/undo`, for one chosen change set. Refused (ChangeConflict) when the
    rows it touched changed afterwards."""
    async with get_db().tenant_session(business_id) as s:
        found = (
            await s.execute(text("SELECT 1 FROM knowledge_change_sets WHERE id = :id"), {"id": change_set_id})
        ).first()
    if found is None:
        raise LookupError("change set not found")
    inverse = await knowledge.revert(business_id, change_set_id)
    return {"undo_change_set_id": str(inverse.id), "summary": inverse.summary}


# ---------------------------------------------------------------- imports & calls


async def imports(business_id: UUID, limit: int = 30) -> list[dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT r.id, r.source_kind, r.status, r.stats, r.change_set_id, r.workflow_id, r.error, r.created_at,
                   r.finished_at, f.filename,
                   coalesce(json_agg(json_build_object('extracted', i.extracted, 'confidence', i.confidence,
                                                       'issues', i.issues, 'resolution', i.resolution)
                                     ORDER BY i.created_at) FILTER (WHERE i.id IS NOT NULL), '[]') AS items
            FROM import_runs r
            LEFT JOIN owner_files f ON f.id = r.source_file_id
            LEFT JOIN import_items i ON i.import_run_id = r.id
            GROUP BY r.id, f.filename ORDER BY r.created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "source_kind": r.source_kind,
            "filename": r.filename,
            "status": str(r.status),
            "stats": r.stats or {},
            "change_set_id": str(r.change_set_id) if r.change_set_id else None,
            "workflow_id": r.workflow_id,
            "error": r.error,
            "created_at": _iso(r.created_at),
            "finished_at": _iso(r.finished_at),
            "items": r.items,
        }
        for r in rows
    ]


async def calls(business_id: UUID, limit: int = 30) -> list[dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT c.id, c.direction, c.path, c.status, c.caller_e164, c.started_at, c.connected_at, c.ended_at,
                   c.end_reason, c.summary, c.transcript, c.conversation_id, cu.display_name, cu.phone_e164,
                   coalesce(json_agg(json_build_object('kind', e.kind, 'status', e.status, 'at', e.created_at,
                                                       'payload', e.payload) ORDER BY e.created_at)
                            FILTER (WHERE e.id IS NOT NULL), '[]') AS events
            FROM call_sessions c
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN call_events e ON e.call_session_id = c.id
            GROUP BY c.id, cu.display_name, cu.phone_e164 ORDER BY c.created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    out = []
    for r in rows:
        end = r.ended_at or None
        seconds = (end - r.connected_at).total_seconds() if end and r.connected_at else None
        out.append(
            {
                "id": str(r.id),
                "direction": str(r.direction),
                "path": str(r.path),
                "status": str(r.status),
                "caller": _who(r.display_name, r.phone_e164 or r.caller_e164),
                "started_at": _iso(r.started_at),
                "seconds": round(seconds) if seconds is not None else None,
                "end_reason": r.end_reason,
                "summary": r.summary,
                "transcript": r.transcript or [],
                "conversation_id": str(r.conversation_id) if r.conversation_id else None,
                "events": r.events,
            }
        )
    return out


# ---------------------------------------------------------------- settings


async def settings_view(business_id: UUID) -> dict[str, Any]:
    """Runtime flags and this business's channel connections (read-only here; the reset lives in actions)."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, channel, provider, display_name, roles, customer_mode, job_description, status,
                               external_account_id
                        FROM channel_connections ORDER BY channel""")
            )
        ).all()
    return {
        "flags": await flags.runtime_flags(),
        "connections": [
            {
                "id": str(r.id),
                "channel": str(r.channel),
                "provider": str(r.provider),
                "display_name": r.display_name,
                "roles": [str(x) for x in r.roles],
                "customer_mode": str(r.customer_mode),
                "job_description": r.job_description,
                "status": str(r.status),
                "account": mask_phone_numbers(r.external_account_id or ""),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- customers (F13: one customer across channels)


async def customers(business_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""
            SELECT c.id, c.display_name, c.phone_e164, c.language, c.last_seen_at, c.created_at,
                   (SELECT coalesce(json_agg(json_build_object('channel', i.channel, 'linked_via', i.linked_via)
                                              ORDER BY i.created_at), '[]')
                      FROM identity_map i WHERE i.customer_id = c.id) AS identities,
                   (SELECT count(*) FROM orders o WHERE o.customer_id = c.id AND o.status <> 'draft') AS orders,
                   (SELECT count(*) FROM customers m WHERE m.merged_into = c.id) AS merged
            FROM customers c WHERE c.merged_into IS NULL
            ORDER BY c.last_seen_at DESC NULLS LAST LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "name": _who(r.display_name, r.phone_e164),
            "language": r.language,
            "identities": r.identities,
            "orders": int(r.orders),
            "merged": int(r.merged),
            "last_seen_at": _iso(r.last_seen_at),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def customer_detail(business_id: UUID, customer_id: UUID, limit: int = 200) -> dict[str, Any] | None:
    """One customer with every identity (and how it was linked), memory, orders, and the merged history: the
    messages of all their conversations on every channel, in time order (CHK-F13-1)."""
    async with get_db().tenant_session(business_id) as s:
        c = (
            await s.execute(
                text("""SELECT id, display_name, phone_e164, language, tags, memory, summary, merged_into,
                               first_seen_at, last_seen_at FROM customers WHERE id = :id"""),
                {"id": customer_id},
            )
        ).first()
        if c is None:
            return None
        identities = (
            await s.execute(
                text("""SELECT channel, external_id, display_name, linked_via, last_inbound_at, created_at
                        FROM identity_map WHERE customer_id = :id ORDER BY created_at"""),
                {"id": customer_id},
            )
        ).all()
        merged = (
            await s.execute(
                text("SELECT id, display_name, created_at FROM customers WHERE merged_into = :id ORDER BY created_at"),
                {"id": customer_id},
            )
        ).all()
        order_rows = (
            await s.execute(
                text("""SELECT id, order_number, status, total_paise, source_channel, created_at FROM orders
                        WHERE customer_id = :id AND status <> 'draft' ORDER BY created_at DESC LIMIT 20"""),
                {"id": customer_id},
            )
        ).all()
        history = (
            await s.execute(
                text("""SELECT m.created_at, m.direction, m.author, m.content_text, cv.channel, cv.id AS conversation_id
                        FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
                        WHERE cv.customer_id = :id ORDER BY m.created_at DESC LIMIT :n"""),
                {"id": customer_id, "n": limit},
            )
        ).all()
    return {
        "id": str(c.id),
        "name": _who(c.display_name, c.phone_e164),
        "phone": mask_phone_numbers(c.phone_e164) if c.phone_e164 else None,
        "language": c.language,
        "tags": list(c.tags or []),
        "facts": (c.memory or {}).get("facts", []),
        "summary": c.summary,
        "merged_into": str(c.merged_into) if c.merged_into else None,
        "first_seen_at": _iso(c.first_seen_at),
        "last_seen_at": _iso(c.last_seen_at),
        "identities": [
            {
                "channel": str(i.channel),
                "external_id": mask_phone_numbers(i.external_id),
                "display_name": i.display_name,
                "linked_via": i.linked_via,
                "last_inbound_at": _iso(i.last_inbound_at),
                "created_at": _iso(i.created_at),
            }
            for i in identities
        ],
        "merged_from": [{"id": str(m.id), "name": m.display_name, "created_at": _iso(m.created_at)} for m in merged],
        "orders": [
            {
                "id": str(o.id),
                "order_number": o.order_number,
                "status": str(o.status),
                "total_rupees": _rupees(o.total_paise),
                "channel": str(o.source_channel) if o.source_channel else None,
                "created_at": _iso(o.created_at),
            }
            for o in order_rows
        ],
        "history": [
            {
                "at": _iso(h.created_at),
                "direction": str(h.direction),
                "author": str(h.author),
                "text": mask_phone_numbers(h.content_text or ""),
                "channel": str(h.channel),
                "conversation_id": str(h.conversation_id),
            }
            for h in reversed(history)
        ],
    }
