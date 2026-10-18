"""Public interface of the `vault` module (BETA_10 §4; ADR-011). Other modules import only from here.

* `put` encrypts a secret (a JSON object) with AES-256-GCM under the active key, bound to its business and row by
  the AAD `business_id|credential_id`; `get` decrypts it and writes `credential_access_log` (purpose, accessor, ok)
  every time, also when it fails.
* `rotate` replaces a secret with a new row (the old one `rotated`); `revoke` makes it unusable at once (the row is
  deleted 30 days later); `seal`/`unseal` protect short-lived secrets kept in other tables (connect flows).
* Jobs: `vault.refresh_due` (hourly) refreshes credentials whose `meta.refresh_after` has passed, through the
  refresher registered for their kind, and deletes revoked rows past their grace; `vault.rewrap` (daily) re-encrypts
  rows still on an older key, in batches of 500.
* Secrets never appear in logs, traces, X-ray, errors or the console.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.config import Settings, get_settings
from reflexo.common.contracts import JobHandlerRegistry, JobRow
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.ports import Registry
from reflexo.vault.crypto import DecryptError, KeyRing, KeyRingError, Sealed, new_key
from reflexo.vault.schemas import CredentialMeta

__all__ = [
    "CredentialMeta",
    "KeyRingError",
    "VaultError",
    "check_keys",
    "get",
    "keyring",
    "new_key",
    "purge_revoked",
    "put",
    "refresh_due",
    "register",
    "register_refresher",
    "revoke",
    "rewrap",
    "rotate",
    "seal",
    "unseal",
]

log = get_logger(__name__)
REWRAP_BATCH = 500
REVOKED_GRACE_DAYS = 30
REFRESH_EVERY_S = 3600
Refresher = Callable[[UUID, dict[str, Any], CredentialMeta], Awaitable[tuple[dict[str, Any], CredentialMeta]]]
_refreshers: dict[str, Refresher] = {}


class VaultError(Exception):
    """A credential that cannot be used. Codes: CREDENTIAL_INVALID (missing, revoked or does not decrypt)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@lru_cache(maxsize=1)
def keyring() -> KeyRing:
    s = get_settings()
    return KeyRing(s.vault_keys.get_secret_value(), s.vault_active_key_id)


def check_keys(settings: Settings | None = None) -> None:
    """Startup (BETA_10 §4): outside development the active vault key must exist, be 32 bytes and not be an
    archive key. Raises `KeyRingError`."""
    s = settings or get_settings()
    if s.env == "dev":
        return
    ring = KeyRing(s.vault_keys.get_secret_value(), s.vault_active_key_id)
    archive = s.archive_keys.get_secret_value()
    if archive and s.archive_active_key_id:
        if ring.raw() & KeyRing(archive, s.archive_active_key_id).raw():
            raise KeyRingError("a vault key is reused as an archive key")


def _aad(business_id: UUID, credential_id: UUID) -> bytes:
    return f"{business_id}|{credential_id}".encode()


# ---------------------------------------------------------------- credentials


async def put(
    business_id: UUID,
    kind: str,
    secret: dict[str, Any],
    label: str,
    *,
    meta: CredentialMeta | None = None,
    session: AsyncSession | None = None,
) -> UUID:
    """Store a secret; returns its credential id. With `session`, in the caller's transaction."""
    credential_id = new_id()
    sealed = keyring().encrypt(json.dumps(secret).encode(), _aad(business_id, credential_id))
    params = {
        "id": credential_id,
        "b": business_id,
        "kind": kind,
        "label": label,
        "key": sealed.key_id,
        "nonce": sealed.nonce,
        "ct": sealed.ciphertext,
        "meta": (meta or CredentialMeta()).model_dump_json(),
    }
    sql = text("""INSERT INTO credential_vault (id, business_id, kind, label, key_id, nonce, ciphertext, meta)
                  VALUES (:id, :b, CAST(:kind AS credential_kind), :label, :key, :nonce, :ct, CAST(:meta AS jsonb))""")
    if session is not None:
        await session.execute(sql, params)
    else:
        async with get_db().tenant_session(business_id) as s:
            await s.execute(sql, params)
    return credential_id


async def get(credential_id: UUID, *, purpose: str, accessor: str) -> dict[str, Any]:
    """Decrypt a credential for one use. Every call writes `credential_access_log`, also when it fails."""
    async with get_db().service_session(ServiceReason.VAULT) as s:
        row = (
            await s.execute(
                text("""SELECT business_id, key_id, nonce, ciphertext, status FROM credential_vault
                        WHERE id = :id"""),
                {"id": credential_id},
            )
        ).first()
        if row is None:
            raise VaultError("CREDENTIAL_INVALID", "unknown credential")
        secret: dict[str, Any] | None = None
        if str(row.status) == "active":
            try:
                plain = keyring().decrypt(
                    Sealed(row.key_id, bytes(row.nonce), bytes(row.ciphertext)), _aad(row.business_id, credential_id)
                )
                secret = json.loads(plain)
            except (DecryptError, KeyRingError, ValueError):
                log.error("vault_decrypt_failed", credential_id=str(credential_id), purpose=purpose)
        await s.execute(
            text("""WITH used AS (UPDATE credential_vault SET last_used_at = now() WHERE id = :id AND :ok)
                    INSERT INTO credential_access_log (id, business_id, credential_id, purpose, accessor, ok)
                    VALUES (:log, :b, :id, :purpose, :accessor, :ok)"""),
            {
                "id": credential_id,
                "log": new_id(),
                "b": row.business_id,
                "purpose": purpose[:200],
                "accessor": accessor[:200],
                "ok": secret is not None,
            },
        )
    if secret is None:
        raise VaultError("CREDENTIAL_INVALID", f"credential is {row.status} or does not decrypt")
    return secret


async def rotate(credential_id: UUID, new_secret: dict[str, Any], *, meta: CredentialMeta | None = None) -> UUID:
    """A new credential replaces the old one (marked `rotated`); returns the new id. Callers repoint to it."""
    async with get_db().service_session(ServiceReason.VAULT) as s:
        old = (
            await s.execute(
                text(
                    "SELECT business_id, kind, label, meta FROM credential_vault WHERE id = :id AND status = 'active'"
                ),
                {"id": credential_id},
            )
        ).first()
        if old is None:
            raise VaultError("CREDENTIAL_INVALID", "only an active credential can be rotated")
        new = await put(
            old.business_id, str(old.kind), new_secret, old.label, meta=meta or CredentialMeta.model_validate(old.meta),
            session=s,
        )  # fmt: skip
        await s.execute(
            text("UPDATE credential_vault SET rotated_from = :old, updated_at = now() WHERE id = :new"),
            {"old": credential_id, "new": new},
        )
        await s.execute(
            text("UPDATE credential_vault SET status = 'rotated', updated_at = now() WHERE id = :id"),
            {"id": credential_id},
        )
    return new


async def revoke(credential_id: UUID) -> None:
    """Unusable at once; the row is deleted after the grace period (`purge_revoked`)."""
    async with get_db().service_session(ServiceReason.VAULT) as s:
        await s.execute(
            text("UPDATE credential_vault SET status = 'revoked', updated_at = now() WHERE id = :id"),
            {"id": credential_id},
        )


# ---------------------------------------------------------------- short-lived secrets in other tables


def seal(business_id: UUID, data: dict[str, Any]) -> bytes:
    """Encrypt a small secret for another table (connect flows): key id, nonce and ciphertext in one blob."""
    sealed = keyring().encrypt(json.dumps(data).encode(), f"{business_id}|seal".encode())
    key_id = sealed.key_id.encode()
    return bytes([len(key_id)]) + key_id + sealed.nonce + sealed.ciphertext


def unseal(business_id: UUID, blob: bytes) -> dict[str, Any]:
    n = blob[0]
    key_id, nonce, ciphertext = blob[1 : 1 + n].decode(), blob[1 + n : 13 + n], blob[13 + n :]
    try:
        plain = keyring().decrypt(Sealed(key_id, nonce, ciphertext), f"{business_id}|seal".encode())
    except (DecryptError, KeyRingError) as exc:
        raise VaultError("CREDENTIAL_INVALID", "sealed data does not open for this business") from exc
    data: dict[str, Any] = json.loads(plain)
    return data


# ---------------------------------------------------------------- maintenance jobs


async def rewrap(batch: int = REWRAP_BATCH) -> int:
    """Re-encrypt every row still on an older key with the active key (BETA_10 §4 rotation). Returns rows done.
    A row that does not decrypt is logged and left as it is: one bad row must not stop a key rotation."""
    ring = keyring()
    done = 0
    skipped: list[UUID] = []
    while True:
        async with get_db().service_session(ServiceReason.VAULT) as s:
            rows = (
                await s.execute(
                    text("""SELECT id, business_id, key_id, nonce, ciphertext FROM credential_vault
                            WHERE key_id <> :active AND NOT (id = ANY(:skip))
                            ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED"""),
                    {"active": ring.active_id, "skip": skipped, "n": batch},
                )
            ).all()
            for r in rows:
                aad = _aad(r.business_id, r.id)
                try:
                    plain = ring.decrypt(Sealed(r.key_id, bytes(r.nonce), bytes(r.ciphertext)), aad)
                except (DecryptError, KeyRingError):
                    log.error("vault_rewrap_skipped", credential_id=str(r.id), key_id=r.key_id)
                    skipped.append(r.id)
                    continue
                sealed = ring.encrypt(plain, aad)
                await s.execute(
                    text("""UPDATE credential_vault SET key_id = :k, nonce = :n, ciphertext = :c, updated_at = now()
                            WHERE id = :id"""),
                    {"k": sealed.key_id, "n": sealed.nonce, "c": sealed.ciphertext, "id": r.id},
                )
                done += 1
        if len(rows) < batch:
            break
    if done or skipped:
        log.info("vault_rewrapped", rows=done, skipped=len(skipped), key_id=ring.active_id)
    return done


def register_refresher(kind: str, refresher: Refresher) -> None:
    """A channel or connector module registers how to refresh its kind of token (Instagram, Meta, MCP OAuth)."""
    _refreshers[kind] = refresher


async def refresh_due() -> int:
    """Refresh credentials whose `meta.refresh_after` has passed, through their kind's refresher (BETA_03 §2.1)."""
    async with get_db().service_session(ServiceReason.VAULT) as s:
        due = (
            await s.execute(
                text("""SELECT id, kind FROM credential_vault WHERE status = 'active'
                          AND (meta->>'refresh_after') IS NOT NULL
                          AND (meta->>'refresh_after')::timestamptz <= now()"""),
            )
        ).all()
    refreshed = 0
    for row in due:
        refresher = _refreshers.get(str(row.kind))
        if refresher is None:
            continue
        try:
            secret = await get(row.id, purpose="refresh", accessor="vault.refresh_due")
            new_secret, meta = await refresher(row.id, secret, CredentialMeta())
            await rotate(row.id, new_secret, meta=meta)
            refreshed += 1
        except Exception as exc:  # one failing token must not stop the others; health checks report it
            log.warning("vault_refresh_failed", credential_id=str(row.id), error=type(exc).__name__)
    return refreshed


async def purge_revoked(days: int = REVOKED_GRACE_DAYS) -> int:
    async with get_db().service_session(ServiceReason.VAULT) as s:
        gone = (
            await s.execute(
                text("""DELETE FROM credential_vault WHERE status = 'revoked'
                          AND updated_at < now() - make_interval(days => :d) RETURNING id"""),
                {"d": days},
            )
        ).all()
    return len(gone)


def register(registry: Registry) -> None:
    jobs = registry.optional(JobHandlerRegistry)  # type: ignore[type-abstract]
    if jobs is None:
        return

    async def refresh_job(job: JobRow) -> None:
        await refresh_due()
        await purge_revoked()

    async def rewrap_job(job: JobRow) -> None:
        await rewrap()

    jobs.register_periodic("vault.refresh_due", refresh_job, REFRESH_EVERY_S)
    jobs.register_daily("vault.rewrap", rewrap_job, "03:30")
