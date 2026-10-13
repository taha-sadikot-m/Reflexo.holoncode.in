"""Media storage, signed URLs and the `media.fetch` / `gateway.typing` jobs (POC_03 §3.2 steps 6–7).

Files live on a local volume in the POC (`REFLEXO_MEDIA_DIR/{business}/{yyyy}/{mm}/{id}`); providers fetch
outbound media through short-lived HMAC-signed URLs served by the API (`/media/{id}?exp=&sig=`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.contracts import JobEnqueuer, JobRow
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import PriorityClass
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.common.timeutil import utcnow
from reflexo.gateway.adapters.base import ChannelAdapter
from reflexo.gateway.connections import credentials, get_connection
from reflexo.gateway.normalized import InboundMedia

log = get_logger(__name__)

FILE_KIND_BY_MIME = (
    ("image/", "image"),
    ("audio/", "audio"),
    ("application/pdf", "pdf"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml", "spreadsheet"),
    ("application/vnd.ms-excel", "spreadsheet"),
    ("text/csv", "spreadsheet"),
)


def file_kind(mime: str) -> str:
    for prefix, kind in FILE_KIND_BY_MIME:
        if mime.startswith(prefix):
            return kind
    return "document" if mime.startswith(("application/", "text/")) else "other"


class MediaSigner:
    def _sig(self, media_id: UUID, exp: int) -> str:
        secret = get_settings().media_url_secret.get_secret_value().encode()
        return hmac.new(secret, f"{media_id}:{exp}".encode(), hashlib.sha256).hexdigest()

    def url_for(self, media_object_id: UUID) -> str:
        s = get_settings()
        exp = int(time.time()) + s.media_url_ttl_s
        return f"{s.public_base_url}/media/{media_object_id}?exp={exp}&sig={self._sig(media_object_id, exp)}"

    def valid(self, media_id: UUID, exp: int, sig: str) -> bool:
        return exp >= int(time.time()) and hmac.compare_digest(sig, self._sig(media_id, exp))


async def media_path(media_id: UUID) -> tuple[Path, str] | None:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        row = (
            await s.execute(text("SELECT storage_key, mime FROM media_objects WHERE id = :id"), {"id": media_id})
        ).first()
    if row is None:
        return None
    return Path(get_settings().media_dir) / row.storage_key, str(row.mime)


SIM_MEDIA_PREFIX = "sim-media:"  # a simulated phone's upload: stored before the message is sent, never downloaded


async def store_simulated_upload(business_id: UUID, content: bytes, mime: str) -> UUID:
    """A photo or file sent from a simulated phone (Lab, public demo): stored now; the simulated message refers to
    it as `sim-media:<id>` in place of a provider media id."""
    return await store_media(business_id, content, mime, source="inbound", provider_media_id=None)


async def store_media(
    business_id: UUID, content: bytes, mime: str, *, source: str, provider_media_id: str | None
) -> UUID:
    media_id = new_id()
    now = utcnow()
    key = f"{business_id}/{now:%Y}/{now:%m}/{media_id}"
    path = Path(get_settings().media_dir) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""
            INSERT INTO media_objects (id, business_id, storage_key, mime, size_bytes, sha256, source,
                                       provider_media_id)
            VALUES (:id, :b, :k, :m, :size, :sha, :src, :pm)"""),
            {
                "id": media_id,
                "b": business_id,
                "k": key,
                "m": mime,
                "size": len(content),
                "sha": hashlib.sha256(content).hexdigest(),
                "src": source,
                "pm": provider_media_id,
            },
        )
    return media_id


async def _simulated_upload(business_id: UUID, raw_id: str) -> tuple[UUID, str]:
    """The stored upload a simulated message refers to — this business's own (RLS), or the job fails."""
    async with get_db().tenant_session(business_id) as s:
        mime = (
            await s.execute(text("SELECT mime FROM media_objects WHERE id = :id"), {"id": UUID(raw_id)})
        ).scalar_one_or_none()
    if mime is None:
        raise LookupError("simulated upload not found for this business")
    return UUID(raw_id), str(mime)


def register_jobs(registry: Registry, adapters: dict[str, ChannelAdapter]) -> None:
    from reflexo.common.contracts import JobHandlerRegistry

    jobs = registry.require(JobHandlerRegistry)  # type: ignore[type-abstract]

    async def fetch(job: JobRow) -> None:
        p = job.payload
        business_id, message_id = UUID(p["business_id"]), UUID(p["message_id"])
        media = InboundMedia.model_validate(p["media"])
        conn = await get_connection(UUID(p["connection_id"]))
        if conn is None:
            raise LookupError("connection gone")
        if p.get("simulated") and media.provider_media_id.startswith(SIM_MEDIA_PREFIX):
            media_id, mime = await _simulated_upload(
                business_id, media.provider_media_id.removeprefix(SIM_MEDIA_PREFIX)
            )
        else:
            if media.url and conn.provider == "simulator":
                async with httpx.AsyncClient() as client:
                    resp = await client.get(media.url, timeout=30.0, follow_redirects=True)
                    resp.raise_for_status()
                    content = resp.content
                    mime = media.mime or resp.headers.get("content-type", "application/octet-stream")
            else:
                content, mime = await adapters[conn.provider].download_media(credentials(conn), media)
            media_id = await store_media(
                business_id, content, mime, source="inbound", provider_media_id=media.provider_media_id
            )
        kind = file_kind(mime)
        async with get_db().tenant_session(business_id) as s:
            await s.execute(
                text("""
                UPDATE messages SET attachments = attachments || CAST(:a AS jsonb) WHERE id = :m"""),
                {
                    "a": json.dumps(
                        [{"media_object_id": str(media_id), "kind": kind, "mime": mime, "filename": media.filename}]
                    ),
                    "m": message_id,
                },
            )
            if p.get("owner_member_id"):
                await s.execute(
                    text("""
                    INSERT INTO owner_files (id, business_id, member_id, media_object_id, filename, kind,
                                             received_via, source_message_id)
                    VALUES (:id, :b, :mem, :mo, :fn, CAST(:k AS file_kind), CAST(:ch AS channel_type), :msg)"""),
                    {
                        "id": new_id(),
                        "b": business_id,
                        "mem": UUID(p["owner_member_id"]),
                        "mo": media_id,
                        "fn": media.filename,
                        "k": kind if kind != "document" else "document",
                        "ch": conn.channel,
                        "msg": message_id,
                    },
                )
        follow = "media.transcribe" if kind == "audio" else "media.describe"
        if jobs.get(follow) is not None:
            enqueuer = registry.require(JobEnqueuer)  # type: ignore[type-abstract]
            await enqueuer.enqueue(
                follow,
                {
                    "business_id": str(business_id),
                    "message_id": str(message_id),
                    "media_object_id": str(media_id),
                    "owner": bool(p.get("owner_member_id")),
                },
                business_id=business_id,
                priority=PriorityClass.P0,
                dedupe_key=f"{follow}:{media_id}",
            )

    async def typing(job: JobRow) -> None:
        conn = await get_connection(UUID(job.payload["connection_id"]))
        if conn is None or conn.provider == "simulator" or job.payload.get("simulated"):
            return
        try:
            await adapters[conn.provider].mark_read_and_typing(
                credentials(conn), job.payload["recipient"], job.payload.get("external_message_id")
            )
        except httpx.HTTPError as exc:  # best effort: never retry a typing indicator
            log.info("typing_indicator_failed", provider=conn.provider, error=type(exc).__name__)

    jobs.register("media.fetch", fetch)
    jobs.register("gateway.typing", typing)
