"""CLI for seeds. Usage: `python scripts/seed.py load|reset [--full]` (via tasks.py seed / demo-reset)."""

from __future__ import annotations

import argparse
import asyncio

from reflexo.admin.seeding import load, reset
from reflexo.common.config import get_settings
from reflexo.common.db import Database
from reflexo.common.logging import configure_logging


async def _main(action: str, full: bool) -> None:
    s = get_settings()
    configure_logging("reflexo-seed", s.log_level)
    db = Database(s.database_url, s.database_service_url)
    try:
        mode = "full" if full else "onboarding"
        report = await (reset(db, mode) if action == "reset" else load(db, mode))
        for slug, bid in report.businesses.items():
            print(f"{slug:14s} {bid}")
        print(f"done in {report.seconds:.2f}s")
    finally:
        await db.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["load", "reset"])
    p.add_argument("--full", action="store_true", help="Meera's Bakes active with full catalogue")
    a = p.parse_args()
    asyncio.run(_main(a.action, a.full))
