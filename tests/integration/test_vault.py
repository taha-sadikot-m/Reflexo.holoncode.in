"""The credential vault (WP-02; BETA_10 §4; ADR-011) and connections reading their credentials from it."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import reflexo.vault.api as vault
from reflexo.common.config import Settings, get_settings
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.gateway import api as gateway
from reflexo.gateway.connections import credentials, get_connection
from tests.factories import make_tenant

pytestmark = pytest.mark.integration


async def q(db: Database, sql: str, **p: Any) -> list[Any]:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return list((await s.execute(text(sql), p)).all())


@pytest.fixture
def active_key() -> Iterator[Any]:
    """Switch the active vault key (the test key ring holds k1 and k2), restoring k1 afterwards."""

    def use(key_id: str, keys: str | None = None) -> None:
        os.environ["REFLEXO_VAULT_ACTIVE_KEY_ID"] = key_id
        if keys is not None:
            os.environ["REFLEXO_VAULT_KEYS"] = keys
        get_settings.cache_clear()
        vault.keyring.cache_clear()

    saved = (os.environ["REFLEXO_VAULT_KEYS"], os.environ["REFLEXO_VAULT_ACTIVE_KEY_ID"])
    yield use
    use(saved[1], saved[0])


@pytest.mark.check("CHK-SEC-2")
async def test_a_copied_ciphertext_does_not_open_and_every_read_is_logged(db: Database) -> None:
    a, b = await make_tenant(db, "Vault A"), await make_tenant(db, "Vault B")
    cid = await vault.put(a.business_id, "meta_page_token", {"token": "page-secret-a"}, "pages:A")
    assert await vault.get(cid, purpose="send:meta_pages", accessor="test") == {"token": "page-secret-a"}

    copy_id = new_id()  # the same bytes in business B's row
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO credential_vault (id, business_id, kind, label, key_id, nonce, ciphertext)
                    SELECT :new, :b, kind, label, key_id, nonce, ciphertext FROM credential_vault WHERE id = :id"""),
            {"new": copy_id, "b": b.business_id, "id": cid},
        )
    with pytest.raises(vault.VaultError) as copied:
        await vault.get(copy_id, purpose="send:meta_pages", accessor="test")
    assert copied.value.code == "CREDENTIAL_INVALID" and "page-secret-a" not in str(copied.value)

    log = await q(
        db,
        "SELECT credential_id, ok, purpose, accessor FROM credential_access_log WHERE credential_id = ANY(:i)",
        i=[cid, copy_id],
    )
    assert sorted((r.credential_id == cid, r.ok) for r in log) == [(False, False), (True, True)]
    assert all(r.purpose == "send:meta_pages" and r.accessor == "test" for r in log)
    [row] = await q(db, "SELECT ciphertext, label FROM credential_vault WHERE id = :i", i=cid)
    assert b"page-secret-a" not in bytes(row.ciphertext)  # stored encrypted


@pytest.mark.check("CHK-SEC-3")
async def test_rewrap_moves_every_row_to_the_new_key(db: Database, active_key: Any) -> None:
    t = await make_tenant(db, "Rotate")
    ids = [await vault.put(t.business_id, "telegram_bot_token", {"token": f"bot-{i}"}, f"tg:{i}") for i in range(3)]
    broken = new_id()  # a row that no longer decrypts must not stop the rotation of the others
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO credential_vault (id, business_id, kind, label, key_id, nonce, ciphertext)
                    VALUES (:id, :b, 'telegram_bot_token', 'broken', 'k1', :n, :c)"""),
            {"id": broken, "b": t.business_id, "n": b"0" * 12, "c": b"not a ciphertext at all"},
        )
    active_key("k2")
    assert await vault.rewrap() >= 3
    rows = await q(db, "SELECT key_id FROM credential_vault WHERE id = ANY(:i)", i=ids)
    assert {r.key_id for r in rows} == {"k2"}
    assert (await q(db, "SELECT key_id FROM credential_vault WHERE id = :i", i=broken))[0].key_id == "k1"
    k2 = os.environ["REFLEXO_VAULT_KEYS"].split(",")[1]
    active_key("k2", k2)  # the old key removed from the ring
    assert [await vault.get(i, purpose="t", accessor="test") for i in ids] == [{"token": f"bot-{i}"} for i in range(3)]


async def test_revoke_rotate_purge_and_seal(db: Database) -> None:
    t, other = await make_tenant(db, "Life"), await make_tenant(db, "Other")
    cid = await vault.put(t.business_id, "instagram_user_token", {"token": "ig-1"}, "ig")
    new = await vault.rotate(cid, {"token": "ig-2"})
    assert await vault.get(new, purpose="t", accessor="test") == {"token": "ig-2"}
    with pytest.raises(vault.VaultError):  # the rotated one is no longer usable
        await vault.get(cid, purpose="t", accessor="test")
    [row] = await q(db, "SELECT rotated_from FROM credential_vault WHERE id = :i", i=new)
    assert row.rotated_from == cid

    await vault.revoke(new)
    with pytest.raises(vault.VaultError):
        await vault.get(new, purpose="t", accessor="test")
    assert await vault.purge_revoked() == 0  # still within the 30-day grace
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("UPDATE credential_vault SET updated_at = now() - interval '31 days' WHERE id = :i"), {"i": new}
        )
    assert await vault.purge_revoked() == 1

    blob = vault.seal(t.business_id, {"pkce_verifier": "abc"})
    assert b"abc" not in blob and vault.unseal(t.business_id, blob) == {"pkce_verifier": "abc"}
    with pytest.raises(vault.VaultError):
        vault.unseal(other.business_id, blob)


def test_startup_refuses_a_bad_key_outside_development() -> None:
    good = vault.new_key()
    vault.check_keys(Settings(env="dev"))  # development may run without keys
    vault.check_keys(Settings(env="test", vault_keys=f"a:{good}", vault_active_key_id="a"))
    with pytest.raises(vault.KeyRingError):
        vault.check_keys(Settings(env="test", vault_keys="", vault_active_key_id="a"))
    with pytest.raises(vault.KeyRingError):
        vault.check_keys(Settings(env="test", vault_keys="a:c2hvcnQ=", vault_active_key_id="a"))  # 5 bytes
    with pytest.raises(vault.KeyRingError):
        vault.check_keys(
            Settings(
                env="test",
                vault_keys=f"a:{good}",
                vault_active_key_id="a",
                archive_keys=f"z:{good}",
                archive_active_key_id="z",
            )
        )


async def test_connections_read_credentials_by_kind(db: Database) -> None:
    t = await make_tenant(db, "Creds")
    cid = await vault.put(t.business_id, "meta_page_token", {"token": "page-tok"}, "pages")
    platform, own, mirror, sim = new_id(), new_id(), new_id(), new_id()
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO platform_connections (id, kind, channel, provider, external_account_id, display_name)
                    VALUES (:id, 'reflexo_telegram', 'telegram', 'telegram', :e, 'Reflexo bot')"""),
            {"id": platform, "e": f"bot-{platform.hex[-6:]}"},
        )
        for row in (
            (own, "facebook_messenger", "meta_pages", f"page-{own.hex[-6:]}", cid, None, False),
            (mirror, "telegram", "telegram", f"bot-{platform.hex[-6:]}", None, platform, False),
            (sim, "whatsapp", "meta_whatsapp", f"sim-wa-{sim.hex[-6:]}", None, None, True),
        ):
            await s.execute(
                text("""INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id,
                                                         display_name, credential_id, platform_connection_id, simulated)
                        VALUES (:id, :b, CAST(:ch AS channel_type), CAST(:p AS channel_provider), :e, 'x', :c, :pc,
                                :sim)"""),
                dict(zip(("id", "ch", "p", "e", "c", "pc", "sim"), row, strict=True)) | {"b": t.business_id},
            )
    by = {k: await get_connection(v) for k, v in (("own", own), ("mirror", mirror), ("sim", sim))}
    assert (await credentials(by["own"], purpose="send")).token == "page-tok"
    assert (
        await credentials(by["mirror"], purpose="send")
    ).token == get_settings().telegram_bot_token.get_secret_value()
    assert (await credentials(by["sim"], purpose="send")).token == ""
    [logged] = await q(db, "SELECT purpose, ok FROM credential_access_log WHERE credential_id = :c", c=cid)
    assert (logged.purpose, logged.ok) == ("send:meta_pages", True)
    gateway.clear_connection_cache()  # mirrors are never found by their platform account (routing is WP-09)
    assert await gateway.find_connection("telegram", f"bot-{platform.hex[-6:]}", "telegram") is None


@pytest.mark.check("CHK-DB-9")
async def test_app_role_cannot_write_platform_rows_or_read_service_tables(db: Database) -> None:
    t = await make_tenant(db, "Roles")
    with pytest.raises(DBAPIError, match="permission denied"):
        async with db.tenant_session(t.business_id) as s:
            await s.execute(
                text("""INSERT INTO platform_connections (id, kind, channel, provider, external_account_id,
                        display_name) VALUES (:id, 'reflexo_telegram', 'telegram', 'telegram', 'x', 'x')"""),
                {"id": new_id()},
            )
    for table in ("organizations", "benchmark_runs"):  # connector_catalog arrives with WP-17
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db.tenant_session(t.business_id) as s:
                await s.execute(text(f"SELECT count(*) FROM {table}"))
    async with db.tenant_session(t.business_id) as s:  # platform rows are readable, for routing views
        await s.execute(text("SELECT count(*) FROM platform_connections"))


async def test_refresh_due_rotates_through_each_kinds_refresher(db: Database) -> None:
    from datetime import UTC, datetime, timedelta

    t = await make_tenant(db, "Refresh")
    due = vault.CredentialMeta(refresh_after=datetime.now(UTC) - timedelta(minutes=1))
    ig = await vault.put(t.business_id, "instagram_user_token", {"token": "ig-old"}, "ig", meta=due)
    page = await vault.put(t.business_id, "meta_page_token", {"token": "page"}, "page", meta=due)
    bot = await vault.put(t.business_id, "telegram_bot_token", {"token": "bot"}, "bot", meta=due)  # no refresher

    async def refresh_ig(cid: Any, secret: dict[str, Any], meta: vault.CredentialMeta) -> tuple[Any, Any]:
        return {"token": secret["token"].replace("old", "new")}, vault.CredentialMeta(token_type="long_lived")

    async def broken(cid: Any, secret: dict[str, Any], meta: vault.CredentialMeta) -> tuple[Any, Any]:
        raise RuntimeError("provider down")

    vault.register_refresher("instagram_user_token", refresh_ig)
    vault.register_refresher("meta_page_token", broken)
    try:
        assert await vault.refresh_due() >= 1
    finally:
        vault._refreshers.clear()
    rows = {r.label: r for r in await q(db, "SELECT * FROM credential_vault WHERE business_id = :b", b=t.business_id)}
    [new_ig] = [r for r in await q(db, "SELECT id FROM credential_vault WHERE rotated_from = :i", i=ig)]
    assert await vault.get(new_ig.id, purpose="t", accessor="test") == {"token": "ig-new"}
    assert str(rows["page"].status) == "active" and str(rows["bot"].status) == "active"  # failed or skipped
    assert {page, bot} <= {r.id for r in rows.values()}


async def test_errors_and_the_job_registration(db: Database) -> None:
    from reflexo.common.contracts import JobRow
    from reflexo.common.ports import reset_registry
    from reflexo.pipeline import api as pipeline_api
    from reflexo.pipeline.jobs import Jobs

    t = await make_tenant(db, "Errors")
    with pytest.raises(vault.VaultError):
        await vault.get(new_id(), purpose="t", accessor="test")  # unknown
    cid = await vault.put(t.business_id, "webhook_secret", {"secret": "s"}, "hook")
    await vault.revoke(cid)
    with pytest.raises(vault.VaultError):
        await vault.rotate(cid, {"secret": "s2"})  # only an active credential rotates
    await vault.rewrap(batch=1)  # one row per batch: the loop runs to the end
    assert await vault.rewrap(batch=1) == 0  # and then nothing is left on an older key

    registry = reset_registry()
    pipeline_api.register(registry)
    vault.register(registry)
    jobs = registry.require(Jobs)
    for queue in ("vault.refresh_due", "vault.rewrap"):
        handler = jobs.get(queue)
        assert handler is not None
        await handler(JobRow(new_id(), None, queue, {"slot": 1, "run_date": "2026-10-18"}, 1, 5, None))
    queued = await q(db, "SELECT queue FROM jobs WHERE queue LIKE 'vault.%' AND status = 'pending'")
    assert {r.queue for r in queued} >= {"vault.refresh_due", "vault.rewrap"}  # each run schedules the next


def test_key_ring_rejects_bad_keys() -> None:
    from reflexo.vault.crypto import KeyRing, Sealed

    good = vault.new_key()
    ring = KeyRing(f"a:{good}", "a")
    assert ring.ids == frozenset({"a"})
    with pytest.raises(vault.KeyRingError):
        KeyRing("a:not base64!!", "a")
    with pytest.raises(vault.KeyRingError):
        KeyRing(f"a:{good}", "b")  # the active id is not in the ring
    with pytest.raises(vault.KeyRingError):
        ring.decrypt(Sealed("gone", b"0" * 12, b"x"), b"aad")  # a removed key
