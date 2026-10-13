"""Self-serve owner accounts (/signup, /login): a new shop and its owner's login in one step.

A new shop starts in onboarding, like Meera's demo reset: an empty catalogue, its AI not yet answering customers,
and the owner sets it up by chatting with it — send the menu (photo, PDF, Excel or typed), answer a few questions,
approve the preview. Until a real WhatsApp number is connected, the shop's channels are simulated phones in the
owner's app, so the owner can test as a customer and teach as the owner. Passwords are hashed with scrypt.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
import reflexo.owner.api as owner
from reflexo.admin import seeding
from reflexo.admin.limits import Window
from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger

log = get_logger(__name__)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_signups = Window(lambda: get_settings().signup_per_client_per_hour)
_lock = asyncio.Lock()


class SignupRefused(Exception):
    def __init__(self, reason: str, *, status: int = 422, retry_after_s: int | None = None) -> None:
        super().__init__(reason)
        self.reason, self.status, self.retry_after_s = reason, status, retry_after_s


@dataclass(frozen=True)
class Account:
    business_id: UUID
    member_id: UUID
    owner_name: str
    business_name: str


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        given = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=32, n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(given.hex(), digest)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # unknown emails cost the same time as wrong passwords


# ---------------------------------------------------------------- sign-up


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-") or "shop"
    return f"{base}-{secrets.token_hex(2)}"


def _prefix(name: str) -> str:
    """Order numbers read like the shop: "Sharma Sweets" → SS-1001."""
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", name))[:3].upper()
    return initials or "OR"


def _seed(business_name: str, owner_name: str, category: str, area: str, suffix: str) -> dict[str, Any]:
    description = f"{category.strip().capitalize()} in {area.strip()}" if category and area else None
    return {
        "slug": _slug(business_name),
        "name": business_name,
        "default_language": "hi-Latn",
        "order_number_prefix": _prefix(business_name),
        "order_number_start": 1000,
        "settings": {},
        "profile": {
            "identity": {
                "name": business_name,
                "description": description,
                "category": category or None,
                "owner_name": owner_name,
            },
            "contact": {"address": area or None},
        },
        "owner": {"display_name": owner_name, "simulated_whatsapp": seeding.SANDBOX_OWNER_WA},
        "connections": seeding.simulated_whatsapp(suffix, business_name),
        "qa": [],
        "catalog": [],
    }


async def _signups_today() -> int:
    async with get_db().service_session(ServiceReason.ACCOUNTS) as s:
        n = (
            await s.execute(text("SELECT count(*) FROM owner_accounts WHERE created_at > now() - interval '1 day'"))
        ).scalar_one()
    return int(n)


async def signup(
    *, email: str, password: str, business_name: str, owner_name: str, category: str, area: str, client: str
) -> Account:
    settings = get_settings()
    email = email.strip().lower()
    business_name, owner_name = business_name.strip(), owner_name.strip()
    if not settings.signup_enabled:
        raise SignupRefused("Sign-up is closed right now.", status=503)
    if not _EMAIL.match(email):
        raise SignupRefused("That email address doesn't look right.")
    if len(password) < 8:
        raise SignupRefused("Use a password of at least 8 characters.")
    if len(business_name) < 2 or len(owner_name) < 1:
        raise SignupRefused("Tell us your shop's name and your name.")
    async with _lock:
        wait = _signups.wait(client)
        if wait is not None:
            raise SignupRefused("Too many sign-ups from here — try again later.", status=429, retry_after_s=wait)
        if await _signups_today() >= settings.signup_daily_cap:
            raise SignupRefused("Today's sign-ups are full — please try again tomorrow.", status=503)
        async with get_db().service_session(ServiceReason.ACCOUNTS) as s:
            taken = (
                await s.execute(text("SELECT 1 FROM owner_accounts WHERE email = :e"), {"e": email})
            ).scalar_one_or_none()
        if taken:
            raise SignupRefused("An account with this email already exists — log in instead.", status=409)
        business_id = await seeding.create_business(
            get_db(), _seed(business_name, owner_name, category, area, secrets.token_hex(4)), full=False
        )
        async with get_db().service_session(ServiceReason.ACCOUNTS) as s:
            member_id: UUID = (
                await s.execute(
                    text("SELECT id FROM team_members WHERE business_id = :b AND role = 'owner' LIMIT 1"),
                    {"b": business_id},
                )
            ).scalar_one()
            await s.execute(
                text("""INSERT INTO owner_accounts (id, email, password_hash, business_id, member_id)
                        VALUES (:id, :e, :h, :b, :m)"""),
                {"id": new_id(), "e": email, "h": hash_password(password), "b": business_id, "m": member_id},
            )
        _signups.hit(client)
    gateway.clear_connection_cache()
    await _welcome(business_id, member_id)
    log.info("owner_signed_up", business_id=str(business_id))
    return Account(business_id, member_id, owner_name, business_name)


async def _welcome(business_id: UUID, member_id: UUID) -> None:
    """The AI's first message on the owner's phone: what to send to get set up."""
    try:
        async with get_db().service_session(ServiceReason.ACCOUNTS) as s:
            conn_id = (
                await s.execute(
                    text("SELECT id FROM channel_connections WHERE business_id = :b AND channel = 'whatsapp' LIMIT 1"),
                    {"b": business_id},
                )
            ).scalar_one()
        conn = await gateway.get_connection(conn_id)
        assert conn is not None
        async with get_db().tenant_session(business_id) as s:
            conv = await gateway.member_conversation(s, conn, seeding.SANDBOX_OWNER_WA.lstrip("+"), member_id)
            await s.execute(text("UPDATE conversations SET simulated = true WHERE id = :c"), {"c": conv})
        await gateway.ConversationSender().send_blocks(
            business_id,
            conv,
            [{"type": "text", "text": owner.onboarding.WELCOME}],
            origin="system",
            idempotency_base=f"welcome:{business_id}",
        )
    except Exception:  # the shop exists either way; the owner can simply say hi
        log.exception("owner_welcome_failed", business_id=str(business_id))


# ---------------------------------------------------------------- log-in


async def login(email: str, password: str) -> Account | None:
    async with get_db().service_session(ServiceReason.ACCOUNTS) as s:
        row = (
            await s.execute(
                text("""SELECT a.id, a.password_hash, a.business_id, a.member_id, m.display_name, b.name
                        FROM owner_accounts a JOIN team_members m ON m.id = a.member_id
                        JOIN businesses b ON b.id = a.business_id WHERE a.email = :e"""),
                {"e": email.strip().lower()},
            )
        ).first()
    if not verify_password(password, row.password_hash if row else _DUMMY_HASH) or row is None:
        return None
    async with get_db().service_session(ServiceReason.ACCOUNTS) as s:
        await s.execute(text("UPDATE owner_accounts SET last_login_at = now() WHERE id = :id"), {"id": row.id})
    return Account(row.business_id, row.member_id, row.display_name, row.name)
