"""The public demo (/demo): every visitor gets a private, simulated copy of the demo shop.

Why a copy each: the demo is the whole product — chatting and calling as a customer, the owner's phone asking and
learning, the owner's dashboard — and two visitors sharing one shop would see each other's orders, questions and
learned rules. A sandbox is a normal business (slug `demo-…`) whose traffic is all simulated, so it runs the real
pipeline end to end and nothing it does can reach a real customer, owner or provider. Sandboxes are deleted after
`demo_sandbox_hours`. Creation is limited per client address and per day.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

import reflexo.gateway.api as gateway
from reflexo.admin import seeding
from reflexo.admin.limits import Window
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.logging import get_logger

log = get_logger(__name__)

_created = Window(lambda: get_settings().demo_per_client_per_hour)
_lock = asyncio.Lock()


class DemoUnavailable(Exception):
    """Demo creation refused: `reason` is shown to the visitor."""

    def __init__(self, reason: str, retry_after_s: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Sandbox:
    business_id: UUID
    slug: str
    name: str


async def _created_today() -> int:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        n = (
            await s.execute(
                text("""SELECT count(*) FROM businesses
                        WHERE slug LIKE :p AND created_at > now() - interval '1 day'"""),
                {"p": seeding.SANDBOX_PREFIX + "%"},
            )
        ).scalar_one()
    return int(n)


async def create(client: str) -> Sandbox:
    settings = get_settings()
    if not settings.demo_enabled:
        raise DemoUnavailable("The live demo is switched off right now.")
    async with _lock:  # the limits are read-then-act; one creation at a time keeps them exact on this replica
        wait = _created.wait(client)
        if wait is not None:
            raise DemoUnavailable(
                "You've opened several demo shops in the last hour. Please reuse your current one.", wait
            )
        if await _created_today() >= settings.demo_daily_cap:
            raise DemoUnavailable("Today's live demos are fully booked. Please try again tomorrow.")
        await expire()
        suffix = secrets.token_hex(4)
        business_id = await seeding.load_sandbox(get_db(), suffix)
        _created.hit(client)
    gateway.clear_connection_cache()
    log.info("demo_sandbox_created", business_id=str(business_id))
    return Sandbox(business_id, seeding.SANDBOX_PREFIX + suffix, seeding.read_seed("meeras_bakes")["name"])


async def is_sandbox(business_id: UUID) -> bool:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        slug = (
            await s.execute(text("SELECT slug FROM businesses WHERE id = :b"), {"b": business_id})
        ).scalar_one_or_none()
    return bool(slug and slug.startswith(seeding.SANDBOX_PREFIX))


async def expire() -> int:
    """Delete sandboxes older than `demo_sandbox_hours`, stopping their workflows first (as demo reset does)."""
    hours = get_settings().demo_sandbox_hours
    async with get_db().service_session(ServiceReason.SEED) as s:
        slugs = tuple(
            r.slug
            for r in (
                await s.execute(
                    text("""SELECT slug FROM businesses
                            WHERE slug LIKE :p AND created_at < now() - make_interval(hours => :h)"""),
                    {"p": seeding.SANDBOX_PREFIX + "%", "h": hours},
                )
            ).all()
        )
    if not slugs:
        return 0
    workflows = ports.registry.optional(WorkflowPort)  # type: ignore[type-abstract]
    deleted, _, _ = await seeding.remove(get_db(), slugs, workflows)
    log.info("demo_sandboxes_expired", deleted=deleted)
    return deleted
