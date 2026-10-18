"""Move an existing database's environment-backed tokens into the vault (WP-02; BETA_02 §7, migration 0010).

    python scripts/vault_import_env.py

1. Upgrades the database to 0009 (the vault and the new connection columns).
2. For every active connection that still names an environment token group (`credential_ref`) and has no
   `credential_id`, and is neither simulated nor a platform mirror, stores that token with `vault.put` and links it.
3. Creates the platform connections (the Reflexo number and bot, from settings) and a mirror for each showcase shop.
4. Refuses to finish if any active real connection still has no credential; otherwise upgrades to head, where
   0010 drops `credential_ref`.

Token values are never printed; only counts and connection ids.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from sqlalchemy import text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOWCASE = ("meeras-bakes", "kurti-kraft", "eval-bakes")


def alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, check=True)


async def import_tokens() -> int:
    import reflexo.vault.api as vault
    from reflexo.admin import seeding
    from reflexo.common.config import get_settings
    from reflexo.common.db import ServiceReason, get_db

    settings = get_settings()
    imported = 0
    async with get_db().service_session(ServiceReason.SEED) as s:
        rows = (
            await s.execute(
                text("""SELECT id, business_id, provider, display_name, credential_ref FROM channel_connections
                        WHERE status = 'active' AND NOT simulated AND platform_connection_id IS NULL
                          AND credential_id IS NULL AND credential_ref IS NOT NULL""")
            )
        ).all()
        for r in rows:
            if r.credential_ref not in seeding.ENV_TOKENS or str(r.provider) not in seeding.CREDENTIAL_KIND:
                print(f"skipped {r.id}: unknown token group or provider")
                continue
            credential_id = await vault.put(
                r.business_id,
                seeding.CREDENTIAL_KIND[str(r.provider)],
                {"token": seeding.env_token(settings, r.credential_ref)},
                f"{r.provider}:{r.display_name}",
                session=s,
            )
            await s.execute(
                text("UPDATE channel_connections SET credential_id = :c WHERE id = :id"),
                {"c": credential_id, "id": r.id},
            )
            imported += 1
        platforms = await seeding.ensure_platform_connections(s, settings)
        showcase = (
            await s.execute(text("SELECT id FROM businesses WHERE slug = ANY(:s)"), {"s": list(SHOWCASE)})
        ).scalars()
        for business_id in showcase:
            have = {
                r.platform_connection_id
                for r in (
                    await s.execute(
                        text("""SELECT platform_connection_id FROM channel_connections
                                WHERE business_id = :b AND platform_connection_id IS NOT NULL"""),
                        {"b": business_id},
                    )
                ).all()
            }
            await seeding.add_mirrors(s, business_id, [p for p in platforms if p[0] not in have])
        missing = (
            await s.execute(
                text("""SELECT count(*) FROM channel_connections WHERE status = 'active' AND NOT simulated
                          AND provider <> 'simulator' AND platform_connection_id IS NULL
                          AND credential_id IS NULL""")
            )
        ).scalar_one()
    if missing:
        raise SystemExit(f"{missing} active connection(s) still have no credential; 0010 would refuse to run")
    return imported


def main() -> None:
    alembic("upgrade", "0009")
    from reflexo.bootstrap import bootstrap

    bootstrap("reflexo-vault-import")
    count = asyncio.run(import_tokens(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
    print(f"imported {count} credential(s) into the vault")
    alembic("upgrade", "head")
    print("database at head: credential_ref dropped")


if __name__ == "__main__":
    main()
