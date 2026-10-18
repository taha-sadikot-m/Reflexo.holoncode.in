"""Channel connection lookup and credentials (POC_03 §3.2 step 2; BETA_03 §1, §3; BETA_10 §4).

Connections are looked up by (provider, external account id, channel), or by connection id for providers whose
webhook URL carries it (Telegram, simulator). Platform mirrors (one per business for the Reflexo number and bot)
are never found by account: inbound for those accounts routes through the platform connection (WP-09). A 30-second
in-process cache keeps the inbound hot path off the database; connection changes are rare and the TTL bounds
staleness.

Credentials (ADR-018): a business's own connection reads its token from the vault (`vault.get`, logged with the
purpose); a platform mirror uses the platform secrets from settings; a simulated connection has none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.vault.api as vault
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
    credential_id: UUID | None
    platform_connection_id: UUID | None
    simulated: bool
    job_description: str | None
    status: str
    business_status: str
    business_name: str


_SELECT = """
    SELECT c.id, c.business_id, c.channel, c.provider, c.external_account_id, c.display_name, c.roles,
           c.customer_mode, c.credential_id, c.platform_connection_id, c.simulated, c.job_description, c.status,
           b.status AS business_status,
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
        r.credential_id,
        r.platform_connection_id,
        bool(r.simulated),
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
                       AND c.external_account_id = :e AND c.channel = CAST(:ch AS channel_type)
                       AND c.platform_connection_id IS NULL"""
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
                       AND c.external_account_id = :e AND c.platform_connection_id IS NULL
                       ORDER BY c.created_at LIMIT 1"""
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


# the platform secret for each platform connection kind's provider (BETA_01 §13)
_PLATFORM_TOKENS = {"meta_whatsapp": "house_wa_access_token", "telegram": "telegram_bot_token"}


async def credentials(conn: ConnectionInfo, *, purpose: str) -> Credentials:
    """The token for one use of `conn` (`purpose` e.g. 'send', 'download_media'), logged by the vault."""
    if conn.simulated or conn.provider == "simulator":
        return Credentials(token="", account_id=conn.external_account_id)
    if conn.platform_connection_id is not None:
        setting = _PLATFORM_TOKENS.get(conn.provider)
        token = getattr(get_settings(), setting).get_secret_value() if setting else ""
        return Credentials(token=token, account_id=conn.external_account_id)
    if conn.credential_id is None:
        raise vault.VaultError("CREDENTIAL_INVALID", f"connection {conn.id} has no credential")
    secret = await vault.get(conn.credential_id, purpose=f"{purpose}:{conn.provider}", accessor="gateway.connections")
    return Credentials(token=str(secret.get("token", "")), account_id=conn.external_account_id)
