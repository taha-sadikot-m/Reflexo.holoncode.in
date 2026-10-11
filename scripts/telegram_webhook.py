"""Point the Telegram bot at this deployment (POC_03 §2: `/webhooks/telegram/{connection_id}` + secret header).

    uv run python scripts/telegram_webhook.py set https://<public-host> [--drop-pending]
    uv run python scripts/telegram_webhook.py info
    uv run python scripts/telegram_webhook.py delete

The bot token and webhook secret come from settings (REFLEXO_TELEGRAM_BOT_TOKEN / _WEBHOOK_SECRET) and are never
printed. The connection is the telegram `channel_connections` row whose external account is the bot id.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db

ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


async def connection_id(bot_id: str) -> str:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        rows = (
            await s.execute(
                text("""SELECT id FROM channel_connections WHERE provider = 'telegram' AND status = 'active'
                          AND external_account_id = :e"""),
                {"e": bot_id},
            )
        ).all()
    if len(rows) != 1:
        sys.exit(f"expected one active telegram connection for bot {bot_id}, found {len(rows)}")
    return str(rows[0].id)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["set", "info", "delete"])
    ap.add_argument("base_url", nargs="?")
    ap.add_argument("--drop-pending", action="store_true", help="discard updates Telegram is still holding")
    args = ap.parse_args()
    s = get_settings()
    token = s.telegram_bot_token.get_secret_value()
    if not token:
        sys.exit("REFLEXO_TELEGRAM_BOT_TOKEN is not set")
    api = f"{s.telegram_api_base}/bot{token}"
    async with httpx.AsyncClient(timeout=20) as client:
        if args.action == "set":
            if not args.base_url:
                sys.exit("set needs the public base URL")
            secret = s.telegram_webhook_secret.get_secret_value()
            if not secret:
                sys.exit("REFLEXO_TELEGRAM_WEBHOOK_SECRET is not set")
            url = f"{args.base_url.rstrip('/')}/webhooks/telegram/{await connection_id(s.seed_tg_bot_id)}"
            r = await client.post(
                f"{api}/setWebhook",
                json={
                    "url": url,
                    "secret_token": secret,
                    "allowed_updates": ALLOWED_UPDATES,
                    "drop_pending_updates": args.drop_pending,
                },
            )
            print("setWebhook:", r.json().get("ok"), r.json().get("description", ""), "->", url)
        elif args.action == "delete":
            r = await client.post(f"{api}/deleteWebhook")
            print("deleteWebhook:", r.json().get("ok"))
        info = (await client.get(f"{api}/getWebhookInfo")).json().get("result", {})
        print(
            "webhook:",
            info.get("url") or "(none)",
            "| pending:",
            info.get("pending_update_count"),
            "| last error:",
            info.get("last_error_message") or "-",
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
