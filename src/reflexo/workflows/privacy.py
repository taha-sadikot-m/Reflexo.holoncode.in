"""Data-subject requests and platform callbacks (BETA_10 §7, §8.3; BETA_03 §14.3).

WP-22a builds the intake: a `data_requests` row for every request, with the published response period, and Meta's
data-deletion and deauthorize callbacks. The work itself (export, erasure end to end) is WP-22b.

Meta signs both callbacks with the app secret (`signed_request` = base64url(signature) "." base64url(JSON), the
signature an HMAC-SHA256 of the encoded JSON). A Meta user is matched to the businesses whose connections were made
with that user's Facebook login (`channel_connections.connect_meta.meta_user_id`, written by the connect flows).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.enums import DataRequestKind
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.timeutil import utcnow

log = get_logger(__name__)
Subject = Literal["owner", "customer", "staff"]
CODE_PREFIX_NONE = "n-"  # a confirmation for a Meta user we hold nothing about


class BadSignedRequest(ValueError):
    pass


async def record_request(
    business_id: UUID,
    *,
    subject_type: Subject,
    subject_id: UUID,
    kind: DataRequestKind,
    via: str | None = None,
    notes: str | None = None,
) -> UUID:
    """A `data_requests` row, due within the published response period (`REFLEXO_RIGHTS_RESPONSE_DAYS`)."""
    request_id = new_id()
    due = utcnow() + timedelta(days=get_settings().rights_response_days)
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO data_requests (id, business_id, subject_type, subject_id, kind, requested_via, due_at,
                                               notes)
                    VALUES (:id, :b, :st, :sid, CAST(:k AS data_request_kind), CAST(:via AS channel_type), :due,
                            :n)"""),
            {"id": request_id, "b": business_id, "st": subject_type, "sid": subject_id, "k": str(kind),
             "via": via, "due": due, "n": notes},
        )  # fmt: skip
    log.info("data_request_recorded", business_id=str(business_id), kind=str(kind), request_id=str(request_id))
    return request_id


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parse_signed_request(signed_request: str, secret: str) -> dict[str, Any]:
    """Meta's `signed_request`, verified with the app secret; raises BadSignedRequest."""
    if not secret:
        raise BadSignedRequest("no app secret configured")
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = _b64url(encoded_sig)
        data = json.loads(_b64url(payload))
    except (ValueError, TypeError) as exc:
        raise BadSignedRequest("malformed signed_request") from exc
    if not isinstance(data, dict) or str(data.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise BadSignedRequest("unexpected algorithm")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise BadSignedRequest("bad signature")
    if not data.get("user_id"):
        raise BadSignedRequest("no user_id")
    return data


async def _businesses_of(meta_user_id: str) -> list[tuple[UUID, UUID | None]]:
    """(business, owner member) for every business with a connection made by this Meta user."""
    async with get_db().service_session(ServiceReason.PRIVACY) as s:
        rows = (
            await s.execute(
                text("""SELECT DISTINCT c.business_id,
                               (SELECT id FROM team_members t WHERE t.business_id = c.business_id AND t.role = 'owner'
                                ORDER BY created_at LIMIT 1) AS owner_id
                        FROM channel_connections c WHERE c.connect_meta->>'meta_user_id' = :u"""),
                {"u": meta_user_id},
            )
        ).all()
    return [(r.business_id, r.owner_id) for r in rows]


def _none_code(meta_user_id: str) -> str:
    """A confirmation for a user we hold nothing about: verifiable later without storing anything."""
    secret = get_settings().meta_app_secret.get_secret_value().encode()
    digest = hmac.new(secret, f"none:{meta_user_id}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{CODE_PREFIX_NONE}{digest}"


def status_url(code: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/meta/deletion-status/{code}"


async def meta_data_deletion(signed_request: str) -> dict[str, str]:
    """Meta's data-deletion callback: one deletion request per business this user connected; returns the status
    page and the confirmation code Meta shows the user."""
    data = parse_signed_request(signed_request, get_settings().meta_app_secret.get_secret_value())
    user = str(data["user_id"])
    ids = []
    for business_id, owner_id in await _businesses_of(user):
        ids.append(
            await record_request(
                business_id,
                subject_type="owner",
                subject_id=owner_id or business_id,
                kind=DataRequestKind.DELETION,
                via=None,
                notes="meta data-deletion callback",
            )
        )
    code = str(ids[0]) if ids else _none_code(user)
    return {"url": status_url(code), "confirmation_code": code}


async def meta_deauthorize(signed_request: str) -> int:
    """Meta's deauthorize callback: the user removed the app. Their connections stop (disconnected, credentials
    revoked by the disconnect job) and a deletion request is recorded for each business. Returns connections
    disconnected."""
    data = parse_signed_request(signed_request, get_settings().meta_app_secret.get_secret_value())
    user = str(data["user_id"])
    async with get_db().service_session(ServiceReason.PRIVACY) as s:
        stopped = (
            await s.execute(
                text("""UPDATE channel_connections SET status = 'disconnected', disconnected_at = now()
                        WHERE connect_meta->>'meta_user_id' = :u AND status <> 'disconnected'
                        RETURNING id"""),
                {"u": user},
            )
        ).all()
    for business_id, owner_id in await _businesses_of(user):
        await record_request(
            business_id, subject_type="owner", subject_id=owner_id or business_id, kind=DataRequestKind.DELETION,
            notes="meta deauthorize callback",
        )  # fmt: skip
    log.info("meta_deauthorized", connections=len(stopped))
    return len(stopped)


async def deletion_status(code: str) -> Literal["none", "received", "in_progress", "completed", "unknown"]:
    """What the status page shows for a confirmation code."""
    if code.startswith(CODE_PREFIX_NONE):
        return "none"
    try:
        request_id = UUID(code)
    except ValueError:
        return "unknown"
    async with get_db().service_session(ServiceReason.PRIVACY) as s:
        status = (
            await s.execute(text("SELECT status::text FROM data_requests WHERE id = :i"), {"i": request_id})
        ).scalar_one_or_none()
    if status is None:
        return "unknown"
    if status in ("received", "verifying"):
        return "received"
    return "completed" if status == "completed" else "in_progress"


# ---------------------------------------------------------------- public legal pages (BETA_10 §14, BETA_03 §14.3)

PAGES = {
    "privacy": "privacy_notice.md",
    "terms": "seller_terms.md",
    "rights": "rights.md",
    "data-deletion": "data_deletion.md",
}
CUSTOMER_NOTICE = "customer_notice_template.md"
STATUS_WORDS = "deletion_status.yaml"


def _legal(name: str) -> str:
    return (Path(get_settings().legal_dir) / name).read_text(encoding="utf-8")


def page_markdown(page: str) -> str | None:
    """A public legal page's Markdown (None: no such page)."""
    return _legal(PAGES[page]) if page in PAGES else None


def status_words(status: str) -> str:
    words = yaml.safe_load(_legal(STATUS_WORDS)) or {}
    return str(words.get(status) or words["unknown"])


async def shop_notice_markdown(slug: str) -> str | None:
    """The customer notice of one shop (`/{slug}/privacy`), from the template and the shop's own details."""
    import reflexo.knowledge.api as knowledge

    async with get_db().service_session(ServiceReason.PRIVACY) as s:
        row = (await s.execute(text("SELECT id, name FROM businesses WHERE slug = :s"), {"s": slug})).first()
    if row is None:
        return None
    contact = (await knowledge.get_profile(row.id)).contact
    reach = contact.phone or contact.website or contact.address or get_settings().privacy_email
    values = {"shop_name": row.name, "shop_contact": reach, "slug": slug}
    body = _legal(CUSTOMER_NOTICE)
    for key, value in values.items():
        body = body.replace("{" + key + "}", str(value))
    return body


def legal_version(page: str) -> str:
    r"""The version a legal page carries in its header (`Version \`...\``): what a consent record stores."""
    match = re.search(r"Version `([^`]+)`", page_markdown(page) or "")
    return match.group(1) if match else "unversioned"


async def record_consent(
    business_id: UUID,
    *,
    subject_type: Subject,
    subject_id: UUID,
    purpose: str,
    granted: bool,
    method: str,
    evidence: dict[str, Any],
) -> UUID:
    consent_id = new_id()
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO consent_records (id, business_id, subject_type, subject_id, purpose, granted, method,
                                                 evidence)
                    VALUES (:id, :b, :st, :sid, :p, :g, :m, CAST(:e AS jsonb))"""),
            {"id": consent_id, "b": business_id, "st": subject_type, "sid": subject_id, "p": purpose, "g": granted,
             "m": method, "e": json.dumps(evidence)},
        )  # fmt: skip
    return consent_id
