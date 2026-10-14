"""The owner's file library (POC_04 §8, F07): every owner attachment, any channel, with an auto description."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import get_db
from reflexo.common.ids import new_id


@dataclass(frozen=True)
class OwnerFile:
    id: UUID
    filename: str | None
    kind: str
    description: str | None
    received_via: str
    created_at: datetime
    media_object_id: UUID

    def line(self, tz_fmt: str) -> str:
        name = f' "{self.filename}"' if self.filename else ""
        desc = self.description or "not described yet"
        return f"[file:{self.id}] {self.kind}{name} - {desc} - {self.received_via}, {tz_fmt}"


async def search_files(
    business_id: UUID,
    *,
    query: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    limit: int = 10,
) -> list[OwnerFile]:
    """Newest first, matched against description and filename."""
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT id, filename, kind, description, received_via, created_at, media_object_id
                        FROM owner_files
                        WHERE (CAST(:k AS file_kind) IS NULL OR kind = CAST(:k AS file_kind))
                          AND (CAST(:since AS timestamptz) IS NULL OR created_at >= CAST(:since AS timestamptz))
                          AND (CAST(:q AS text) IS NULL OR COALESCE(description, '') || ' ' || COALESCE(filename, '')
                               ILIKE '%' || CAST(:q AS text) || '%')
                        ORDER BY created_at DESC LIMIT :n"""),
                {"k": kind, "since": since, "q": query, "n": limit},
            )
        ).all()
    return [
        OwnerFile(r.id, r.filename, str(r.kind), r.description, str(r.received_via), r.created_at, r.media_object_id)
        for r in rows
    ]


async def set_description(business_id: UUID, media_object_id: UUID, description: str) -> int:
    async with get_db().tenant_session(business_id) as s:
        return len(
            (
                await s.execute(
                    text("""UPDATE owner_files SET description = :d WHERE media_object_id = :m RETURNING id"""),
                    {"d": description[:1000], "m": media_object_id},
                )
            ).all()
        )


async def member_info(business_id: UUID, member_id: UUID) -> tuple[str, str]:
    """(display name, role) of a team member."""
    async with get_db().tenant_session(business_id) as s:
        row = (
            await s.execute(text("SELECT display_name, role FROM team_members WHERE id = :m"), {"m": member_id})
        ).first()
    return (row.display_name, str(row.role)) if row else ("Owner", "owner")


TYPED_SOURCE_MIME = "text/plain"


async def save_typed_source(business_id: UUID, member_id: UUID | None, text_: str, channel: str) -> UUID:
    """A menu or price list the owner typed instead of sending a file: kept in the file library like one, so the
    same import reads it (and it can be found and re-imported later). Returns the owner file id."""
    import reflexo.gateway.api as gateway

    media_id = await gateway.store_media(
        business_id, text_.encode(), TYPED_SOURCE_MIME, source="owner_upload", provider_media_id=None
    )
    file_id = new_id()
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO owner_files (id, business_id, member_id, media_object_id, filename, kind, description,
                                             received_via)
                    VALUES (:id, :b, :m, :mo, 'typed-menu.txt', 'document', :d, CAST(:ch AS channel_type))"""),
            {
                "id": file_id,
                "b": business_id,
                "m": member_id,
                "mo": media_id,
                "d": "Menu / price list typed in chat",
                "ch": channel,
            },
        )
    return file_id


async def get_file(business_id: UUID, file_id: UUID) -> OwnerFile | None:
    async with get_db().tenant_session(business_id) as s:
        r = (
            await s.execute(
                text("""SELECT id, filename, kind, description, received_via, created_at, media_object_id
                        FROM owner_files WHERE id = :id"""),
                {"id": file_id},
            )
        ).first()
    return (
        OwnerFile(r.id, r.filename, str(r.kind), r.description, str(r.received_via), r.created_at, r.media_object_id)
        if r
        else None
    )
