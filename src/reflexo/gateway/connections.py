"""Channel connection lookup and credentials (POC_03 §3.2 step 2; POC_01 §8).

Connections are looked up by (provider, external account id, channel), or by connection id for providers whose
webhook URL carries it (Telegram, simulator). A 30-second in-process cache keeps the inbound hot path off the
database; connection changes are rare and the TTL bounds staleness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.gateway.adapters.base import Credentials

_CACHE_TTL_S = 30.0
_MISS_TTL_S = 5.0  # unknown accounts are re-checked quickly so a newly added connection works at once
_cache: dict[tuple[str, str, str], tuple[float, ConnectionInfo | None]] = {}

ID_ROUTED_PROVIDERS = frozenset({"telegram", "simulator"})


@dataclass(frozen=True)
class ConnectionInfo:
    id: UUID
    business_id: UUID
    channel: str
    provider: str
    external_account_id: str
    display_name: str
    roles: tuple[str, ...]
    customer_mode: str
    credential_ref: str | None
    job_description: str | None
    status: str
    business_status: str
    business_name: str


_SELECT = """
    SELECT c.id, c.business_id, c.channel, c.provider, c.external_account_id, c.display_name, c.roles,
           c.customer_mode, c.credential_ref, c.job_description, c.status, b.status AS business_status,
           b.name AS business_name
    FROM channel_connections c JOIN businesses b ON b.id = c.business_id
"""


def _info(r: Any) -> ConnectionInfo:
    return ConnectionInfo(
        r.id,
        r.business_id,
        str(r.channel),
        str(r.provider),
        r.external_account_id,
        r.display_name,
        tuple(str(x) for x in r.roles),
        str(r.customer_mode),
        r.credential_ref,
        r.job_description,
        str(r.status),
        str(r.business_status),
        r.business_name,
    )


async def find_connection(provider: str, route_key: str, channel: str | None = None) -> ConnectionInfo | None:
    key = (provider, route_key, channel or "")
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < (_CACHE_TTL_S if hit[1] is not None else _MISS_TTL_S):
        return hit[1]
    async with get_db().service_session(ServiceReason.INBOUND_RESOLVE) as s:
        if provider in ID_ROUTED_PROVIDERS:
            try:
                conn_id = UUID(route_key)
            except ValueError:
                return None
            row = (
                await s.execute(
                    text(_SELECT + " WHERE c.id = :id AND c.provider = CAST(:p AS channel_provider)"),
                    {"id": conn_id, "p": provider},
                )
            ).first()
        elif channel:
            row = (
                await s.execute(
                    text(
                        _SELECT
                        + """ WHERE c.provider = CAST(:p AS channel_provider)
                       AND c.external_account_id = :e AND c.channel = CAST(:ch AS channel_type)"""
                    ),
                    {"p": provider, "e": route_key, "ch": channel},
                )
            ).first()
        else:
            row = (
                await s.execute(
                    text(
                        _SELECT
                        + """ WHERE c.provider = CAST(:p AS channel_provider)
                       AND c.external_account_id = :e ORDER BY c.created_at LIMIT 1"""
                    ),
                    {"p": provider, "e": route_key},
                )
            ).first()
    info = _info(row) if row is not None else None
    _cache[key] = (now, info)
    return info


async def get_connection(connection_id: UUID) -> ConnectionInfo | None:
    async with get_db().service_session(ServiceReason.INBOUND_RESOLVE) as s:
        row = (await s.execute(text(_SELECT + " WHERE c.id = :id"), {"id": connection_id})).first()
    return _info(row) if row is not None else None


def clear_cache() -> None:
    _cache.clear()


def credentials(conn: ConnectionInfo) -> Credentials:
    """POC: credential_ref names an env-backed setting group; production uses the vault (specs_v2 D-066)."""
    s = get_settings()
    tokens = {
        "REFLEXO_WA": s.wa_access_token.get_secret_value(),
        "REFLEXO_FB": s.fb_page_token.get_secret_value(),
        "REFLEXO_IG": s.ig_access_token.get_secret_value(),
        "REFLEXO_TELEGRAM": s.telegram_bot_token.get_secret_value(),
    }
    return Credentials(token=tokens.get(conn.credential_ref or "", ""), account_id=conn.external_account_id)
