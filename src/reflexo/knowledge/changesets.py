"""Knowledge change sets (POC_05 §1.4): draft → preview → apply (one transaction) → revert (inverse set).

* `before` is captured automatically when an item is added; `apply` re-checks it against the current row and
  refuses the whole set if anything changed meanwhile (`ChangeConflict`), so a read-back the owner confirmed is
  exactly what gets applied.
* Deletes are soft (`status='archived'`), so undo can always restore.
* Search text for affected entities is rebuilt inside the same transaction; embeddings follow via a job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.contracts import DomainEventWriter, JobEnqueuer, NewDomainEvent
from reflexo.common.db import get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.money import format_inr
from reflexo.common.ports import Registry
from reflexo.knowledge import indexer
from reflexo.knowledge.models import (
    BusinessProfile,
    ChangeConflict,
    ChangeItem,
    ChangeOp,
    ChangeSet,
    ChangeSetPreview,
    EntityType,
)

log = get_logger(__name__)

# column → SQL type for casting (None = bind natively)
_OFFERING = {
    "kind": "offering_kind", "name": None, "description": None, "category": None, "attributes": "jsonb",
    "base_price_paise": None, "stock_mode": "stock_mode", "stock_qty": None, "status": "record_status",
    "aliases": None, "image_media_id": "uuid", "source": None, "external_ref": None,
}  # fmt: skip
_VARIANT = {
    "offering_id": "uuid", "name": None, "sku": None, "option_values": "jsonb", "price_paise": None,
    "stock_qty": None, "status": "record_status", "external_ref": None,
}  # fmt: skip
_QA = {
    "question": None, "question_variants": None, "answer": None, "scope": "answer_scope", "customer_id": "uuid",
    "valid_until": "timestamptz", "topic": "topic", "source": None, "status": "record_status",
    "source_message_id": "uuid",
}  # fmt: skip
_SCHEMA = {
    "name": None,
    "applies_to": "jsonb",
    "fields": "jsonb",
    "status": "record_status",
    "source_message_id": "uuid",
}


@dataclass(frozen=True)
class _Entity:
    table: str
    columns: dict[str, str | None]
    versioned: bool
    label: str  # column used in read-backs


ENTITIES: dict[str, _Entity] = {
    "offering": _Entity("offerings", _OFFERING, True, "name"),
    "variant": _Entity("offering_variants", _VARIANT, False, "name"),
    "qa": _Entity("qa_entries", _QA, True, "question"),
    "detail_schema": _Entity("order_detail_schemas", _SCHEMA, False, "name"),
}
SOURCE_BY_ORIGIN = {
    "owner_command": "owner_chat", "learning": "owner_chat", "import": "import", "onboarding": "onboarding",
    "admin": "admin", "seed": "seed",
}  # fmt: skip

_registry: Registry | None = None


def bind_registry(registry: Registry) -> None:
    global _registry
    _registry = registry


# ---------------------------------------------------------------- value helpers


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)))


def _same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, str):
        try:
            return datetime.fromisoformat(a) == datetime.fromisoformat(b)  # PG vs Python timestamp formatting
        except ValueError:
            return False
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return float(a) == float(b)
    return False


def _bind(sql_type: str | None, value: Any) -> tuple[str, Any]:
    """SQL expression for a bind parameter `:p` plus the value to bind."""
    if value is None:
        return ("CAST(NULL AS " + sql_type + ")" if sql_type else "NULL"), None
    if sql_type == "jsonb":
        return "CAST(:p AS jsonb)", json.dumps(value, ensure_ascii=False)
    if sql_type == "uuid":
        return ":p", UUID(str(value))
    if sql_type:
        return f"CAST(CAST(:p AS text) AS {sql_type})", str(value)
    return ":p", value


async def _snapshot(s: AsyncSession, business_id: UUID, entity_type: str, entity_id: UUID) -> dict[str, Any] | None:
    if entity_type == "profile":
        row = (
            await s.execute(text("SELECT profile FROM business_profiles WHERE business_id = :b"), {"b": business_id})
        ).first()
        return {"profile": row.profile} if row else None
    ent = ENTITIES[entity_type]
    row = (
        await s.execute(
            text(f"SELECT to_jsonb(t) AS j FROM {ent.table} t WHERE id = :id AND business_id = :b"),  # noqa: S608 - fixed table map
            {"id": entity_id, "b": business_id},
        )
    ).first()
    return dict(row.j) if row else None


# ---------------------------------------------------------------- building a draft


async def begin(
    business_id: UUID,
    origin: str,
    summary: str,
    *,
    member_id: UUID | None = None,
    source_file_id: UUID | None = None,
) -> ChangeSet:
    cs_id = new_id()
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO knowledge_change_sets (id, business_id, origin, summary, created_by_member_id,
                                                       source_file_id)
                    VALUES (:id, :b, CAST(:o AS change_set_origin), :sum, :m, :f)"""),
            {"id": cs_id, "b": business_id, "o": origin, "sum": summary, "m": member_id, "f": source_file_id},
        )
    return ChangeSet(id=cs_id, business_id=business_id, origin=origin, summary=summary, status="draft")


async def add(
    cs: ChangeSet,
    entity_type: EntityType,
    op: ChangeOp,
    entity_id: UUID | None = None,
    after: dict[str, Any] | None = None,
) -> ChangeItem | None:
    """Add one item to a draft. Returns None for an update that changes nothing."""
    if cs.status != "draft":
        raise ValueError("can only add to a draft change set")
    after = _jsonable(after) if after is not None else None
    if entity_type == "profile":
        if op != "update" or not after or set(after) != {"profile"}:
            raise ValueError("profile changes are updates with after={'profile': <full profile>}")
        BusinessProfile.model_validate(after["profile"])
        entity_id = cs.business_id
    else:
        allowed = ENTITIES[entity_type].columns
        unknown = set(after or {}) - set(allowed)
        if unknown:
            raise ValueError(f"unknown {entity_type} fields: {sorted(unknown)}")
    if op == "create":
        if not after:
            raise ValueError("create needs `after`")
        entity_id = entity_id or new_id()
        if entity_type in ("offering", "qa") and "source" not in after:
            after["source"] = SOURCE_BY_ORIGIN.get(cs.origin, "admin")
    elif entity_id is None:
        raise ValueError(f"{op} needs entity_id")

    before: dict[str, Any] | None = None
    async with get_db().tenant_session(cs.business_id) as s:
        if op != "create":
            current = await _snapshot(s, cs.business_id, entity_type, entity_id)
            if current is None:
                raise LookupError(f"{entity_type} {entity_id} not found")
            if op == "update":
                assert after is not None
                before = {k: current.get(k) for k in after}
                if all(_same(before[k], after[k]) for k in after):
                    return None
            else:  # soft delete
                label = ENTITIES[entity_type].label
                before = {"status": current.get("status"), label: current.get(label)}
                after = {"status": "archived"}
        item = ChangeItem(id=new_id(), entity_type=entity_type, entity_id=entity_id, op=op, before=before, after=after)
        await _insert_item(s, cs.id, cs.business_id, item)
    cs.items.append(item)
    return item


async def _insert_item(s: AsyncSession, cs_id: UUID, business_id: UUID, item: ChangeItem) -> None:
    await s.execute(
        text("""INSERT INTO knowledge_change_items (id, business_id, change_set_id, entity_type, entity_id, op,
                                                    before, after)
                VALUES (:id, :b, :cs, :et, :eid, CAST(:op AS change_op), CAST(:before AS jsonb),
                        CAST(:after AS jsonb))"""),
        {
            "id": item.id,
            "b": business_id,
            "cs": cs_id,
            "et": item.entity_type,
            "eid": item.entity_id,
            "op": item.op,
            "before": json.dumps(item.before, ensure_ascii=False) if item.before is not None else None,
            "after": json.dumps(item.after, ensure_ascii=False) if item.after is not None else None,
        },
    )


async def load(business_id: UUID, cs_id: UUID, s: AsyncSession | None = None) -> ChangeSet:
    async def _load(session: AsyncSession) -> ChangeSet:
        row = (
            await session.execute(
                text("SELECT * FROM knowledge_change_sets WHERE id = :id AND business_id = :b"),
                {"id": cs_id, "b": business_id},
            )
        ).one()
        items = (
            await session.execute(
                text("SELECT * FROM knowledge_change_items WHERE change_set_id = :id ORDER BY id"), {"id": cs_id}
            )
        ).all()
        return ChangeSet(
            id=row.id,
            business_id=row.business_id,
            origin=str(row.origin),
            summary=row.summary,
            status=str(row.status),
            revert_of=row.revert_of,
            items=[
                ChangeItem(
                    id=i.id,
                    entity_type=i.entity_type,
                    entity_id=i.entity_id,
                    op=str(i.op),
                    before=i.before,
                    after=i.after,
                )
                for i in items
            ],
        )

    if s is not None:
        return await _load(s)
    async with get_db().tenant_session(business_id) as session:
        return await _load(session)


# ---------------------------------------------------------------- preview (read-back lines)


def _fmt(key: str, value: Any) -> str:
    if value is None:
        return "—"
    if key.endswith("_paise") and isinstance(value, int | float):
        return format_inr(int(value))
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _profile_diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key.startswith("_"):
            continue
        b, a = before.get(key), after.get(key)
        if _same(b, a):
            continue
        path = f"{prefix}{key}"
        if isinstance(b, dict) and isinstance(a, dict) and prefix.count(".") < 2:
            lines += _profile_diff(b, a, path + ".")
        else:
            lines.append(f"✏️ {path.replace('_', ' ')}: {_fmt(key, b)} → {_fmt(key, a)}")
    return lines


async def _variant_offering_id(s: AsyncSession, business_id: UUID, item: ChangeItem) -> UUID | None:
    oid = (item.after or {}).get("offering_id")
    if oid is None:
        snap = await _snapshot(s, business_id, "variant", item.entity_id)
        oid = snap.get("offering_id") if snap else None
    return UUID(str(oid)) if oid else None


async def _label(s: AsyncSession, cs: ChangeSet, item: ChangeItem) -> str:
    ent = ENTITIES[item.entity_type]
    label = (item.after or {}).get(ent.label) or (item.before or {}).get(ent.label)
    if label is None:
        label = ((await _snapshot(s, cs.business_id, item.entity_type, item.entity_id)) or {}).get(ent.label, "?")
    if item.entity_type != "variant":
        return str(label)
    oid = await _variant_offering_id(s, cs.business_id, item)
    if oid is None:
        return str(label)
    pending = next((i.after or {} for i in cs.items if i.entity_type == "offering" and i.entity_id == oid), {})
    parent = (
        pending.get("name")
        or (await s.execute(text("SELECT name FROM offerings WHERE id = :id"), {"id": oid})).scalar_one_or_none()
    )
    return f"{parent} {label}" if parent else str(label)


async def preview(business_id: UUID, cs_id: UUID) -> ChangeSetPreview:
    """Human-readable diff lines for the owner's read-back ("➕ Red Velvet 1 kg — ₹1,100")."""
    lines: list[str] = []
    async with get_db().tenant_session(business_id) as s:
        cs = await load(business_id, cs_id, s)
        for i in cs.items:
            if i.entity_type == "profile":
                lines += _profile_diff((i.before or {}).get("profile", {}), (i.after or {}).get("profile", {}))
                continue
            label = await _label(s, cs, i)
            after = i.after or {}
            if i.op == "create":
                extra = ""
                price = after.get("price_paise", after.get("base_price_paise"))
                if price is not None:
                    extra = f" — {_fmt('price_paise', price)}"
                if i.entity_type == "qa":
                    extra = f" → {after.get('answer', '')}"
                lines.append(f"➕ {'Q&A: ' if i.entity_type == 'qa' else ''}{label}{extra}")
            elif i.op == "delete":
                lines.append(f"🗑 Remove: {label}")
            else:
                for key, new in after.items():
                    name = key.removesuffix("_paise").replace("_", " ")
                    lines.append(f"✏️ {label} {name}: {_fmt(key, (i.before or {}).get(key))} → {_fmt(key, new)}")
    return ChangeSetPreview(change_set_id=cs.id, summary=cs.summary, lines=lines)


# ---------------------------------------------------------------- apply / revert


def _check(cs_id: UUID, item: ChangeItem, current: dict[str, Any] | None) -> None:
    if item.op == "create":
        if current is not None:
            raise ChangeConflict(cs_id, item.entity_type, item.entity_id, ["already exists"])
        return
    if current is None:
        raise ChangeConflict(cs_id, item.entity_type, item.entity_id, ["deleted"])
    keys = ["status"] if item.op == "delete" else list(item.before or {})
    changed = [k for k in keys if not _same(current.get(k), (item.before or {}).get(k))]
    if changed:
        raise ChangeConflict(cs_id, item.entity_type, item.entity_id, changed)


async def _execute(s: AsyncSession, business_id: UUID, item: ChangeItem) -> None:
    after = item.after or {}
    if item.entity_type == "profile":
        await s.execute(
            text("""UPDATE business_profiles SET profile = CAST(:p AS jsonb), version = version + 1,
                                                updated_at = now() WHERE business_id = :b"""),
            {"p": json.dumps(after["profile"], ensure_ascii=False), "b": business_id},
        )
        return
    ent = ENTITIES[item.entity_type]
    params: dict[str, Any] = {"id": item.entity_id, "b": business_id}
    exprs: dict[str, str] = {}
    for n, (col, value) in enumerate(after.items()):
        expr, bound = _bind(ent.columns[col], value)
        exprs[col] = expr.replace(":p", f":p{n}")
        if bound is not None:
            params[f"p{n}"] = bound
    if item.op == "create":
        cols = ", ".join(["id", "business_id", *exprs])
        vals = ", ".join([":id", ":b", *exprs.values()])
        await s.execute(text(f"INSERT INTO {ent.table} ({cols}) VALUES ({vals})"), params)  # noqa: S608 - whitelisted
        return
    sets = [f"{c} = {e}" for c, e in exprs.items()]
    if ent.versioned:
        sets.append("version = version + 1")
    if ent.table != "qa_entries":
        sets.append("updated_at = now()")
    await s.execute(
        text(f"UPDATE {ent.table} SET {', '.join(sets)} WHERE id = :id AND business_id = :b"),  # noqa: S608 - whitelisted
        params,
    )


async def _index_refs(s: AsyncSession, business_id: UUID, items: list[ChangeItem]) -> list[tuple[str, UUID]]:
    refs: list[tuple[str, UUID]] = []
    for i in items:
        if i.entity_type == "variant":
            snap = await _snapshot(s, business_id, "variant", i.entity_id)
            if snap and snap.get("offering_id"):
                refs.append(("offering", UUID(str(snap["offering_id"]))))
        elif i.entity_type in ("offering", "qa", "profile"):
            refs.append((i.entity_type, i.entity_id))
    return refs


async def _apply_in(s: AsyncSession, cs: ChangeSet) -> None:
    for item in cs.items:
        _check(cs.id, item, await _snapshot(s, cs.business_id, item.entity_type, item.entity_id))
        await _execute(s, cs.business_id, item)
    await s.execute(
        text("UPDATE knowledge_change_sets SET status = 'applied', applied_at = now() WHERE id = :id"), {"id": cs.id}
    )
    await indexer.index_entities(s, cs.business_id, await _index_refs(s, cs.business_id, cs.items))
    writer = _registry.optional(DomainEventWriter) if _registry else None  # type: ignore[type-abstract]
    if writer is not None:
        await writer.append(
            s,
            NewDomainEvent(
                business_id=cs.business_id,
                type="knowledge.changed",
                source="knowledge",
                correlation_id=cs.id,
                payload={
                    "change_set_id": str(cs.id),
                    "origin": cs.origin,
                    "summary": cs.summary,
                    "revert_of": str(cs.revert_of) if cs.revert_of else None,
                    "entities": sorted({i.entity_type for i in cs.items}),
                },
            ),
        )
    enqueuer = _registry.optional(JobEnqueuer) if _registry else None  # type: ignore[type-abstract]
    await indexer.enqueue_embedding(enqueuer, cs.business_id, s)


async def apply(business_id: UUID, cs_id: UUID) -> ChangeSet:
    """Apply a draft atomically. Raises ChangeConflict (nothing applied) if a row changed since the draft."""
    async with get_db().tenant_session(business_id) as s:
        status = (
            await s.execute(
                text("SELECT status FROM knowledge_change_sets WHERE id = :id AND business_id = :b FOR UPDATE"),
                {"id": cs_id, "b": business_id},
            )
        ).scalar_one()
        cs = await load(business_id, cs_id, s)
        if str(status) == "applied":
            return cs  # idempotent: a duplicate "YES" does not apply twice
        if str(status) != "draft":
            raise ValueError(f"change set {cs_id} is {status}")
        await _apply_in(s, cs)
        cs.status = "applied"
    log.info("change_set_applied", business_id=str(business_id), change_set_id=str(cs_id), items=len(cs.items))
    return cs


def _inverse(item: ChangeItem) -> ChangeItem:
    if item.op == "create":
        if item.entity_type == "profile":
            raise ValueError("profile cannot be created by a change set")
        return ChangeItem(
            id=new_id(),
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            op="update",
            before={"status": (item.after or {}).get("status", "active")},
            after={"status": "archived"},
        )
    if item.op == "update":
        return ChangeItem(
            id=new_id(),
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            op="update",
            before=item.after,
            after=item.before,
        )
    return ChangeItem(
        id=new_id(),
        entity_type=item.entity_type,
        entity_id=item.entity_id,
        op="update",
        before={"status": "archived"},
        after={"status": (item.before or {}).get("status", "active")},
    )


async def revert(business_id: UUID, cs_id: UUID | None = None, *, member_id: UUID | None = None) -> ChangeSet:
    """Undo the last applied change set (or `cs_id`) by applying its inverse. Raises ChangeConflict if the
    affected rows changed after it was applied, LookupError if there is nothing to undo."""
    async with get_db().tenant_session(business_id) as s:
        if cs_id is None:
            cs_id = (
                await s.execute(
                    text("""SELECT id FROM knowledge_change_sets WHERE business_id = :b AND status = 'applied'
                            ORDER BY applied_at DESC, id DESC LIMIT 1 FOR UPDATE"""),
                    {"b": business_id},
                )
            ).scalar_one_or_none()
            if cs_id is None:
                raise LookupError("nothing to undo")
        else:
            await s.execute(text("SELECT 1 FROM knowledge_change_sets WHERE id = :id FOR UPDATE"), {"id": cs_id})
        target = await load(business_id, cs_id, s)
        if target.status != "applied":
            raise ValueError(f"change set {cs_id} is {target.status}")
        inverse = ChangeSet(
            id=new_id(),
            business_id=business_id,
            origin=target.origin,
            summary=f"Undo: {target.summary}",
            status="draft",
            revert_of=target.id,
            items=[_inverse(i) for i in reversed(target.items)],
        )
        await s.execute(
            text("""INSERT INTO knowledge_change_sets (id, business_id, origin, summary, created_by_member_id,
                                                       revert_of)
                    VALUES (:id, :b, CAST(:o AS change_set_origin), :sum, :m, :r)"""),
            {
                "id": inverse.id,
                "b": business_id,
                "o": inverse.origin,
                "sum": inverse.summary,
                "m": member_id,
                "r": target.id,
            },
        )
        for item in inverse.items:
            await _insert_item(s, inverse.id, business_id, item)
        await _apply_in(s, inverse)
        await s.execute(
            text("UPDATE knowledge_change_sets SET status = 'reverted', reverted_at = now() WHERE id = :id"),
            {"id": target.id},
        )
        inverse.status = "applied"
    log.info("change_set_reverted", business_id=str(business_id), change_set_id=str(cs_id), undo=str(inverse.id))
    return inverse


async def apply_now(
    business_id: UUID,
    origin: str,
    summary: str,
    changes: list[tuple[EntityType, ChangeOp, UUID | None, dict[str, Any] | None]],
    *,
    member_id: UUID | None = None,
) -> ChangeSet:
    """Build and apply in one go (learning from owner answers: the owner is the source, POC_05 §1.4)."""
    cs = await begin(business_id, origin, summary, member_id=member_id)
    for entity_type, op, entity_id, after in changes:
        await add(cs, entity_type, op, entity_id, after)
    return await apply(business_id, cs.id)
