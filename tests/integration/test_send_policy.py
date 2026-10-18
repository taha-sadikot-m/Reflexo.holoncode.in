"""The outbound send policy, rule by rule (POC_03 §9; safety core `reflexo.gateway.policy`, BETA_08 §2.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.db import Database, ServiceReason
from reflexo.common.flags import clear_caches
from reflexo.common.ids import new_id
from reflexo.common.timeutil import FixedClock, SystemClock, set_clock
from reflexo.gateway import policy
from tests.factories import Tenant, make_outbound, make_tenant

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _real_clock() -> Any:
    yield
    set_clock(SystemClock())
    clear_caches()


async def set_(db: Database, sql: str, **p: Any) -> None:
    async with db.service_session(ServiceReason.ADMIN) as s:
        await s.execute(text(sql), p)


async def decide(db: Database, t: Tenant, origin: str = "ai_turn") -> policy.PolicyDecision:
    async with db.tenant_session(t.business_id) as s:
        return await policy.check(
            s, business_id=t.business_id, conversation_id=t.conversation_id, origin=origin, fallback_text="Hi"
        )


async def on_provider(db: Database, t: Tenant, provider: str, channel: str) -> None:
    """Move the tenant's conversation onto a connection of `provider` (the 24-hour window rules)."""
    cid = new_id()
    await set_(
        db,
        """INSERT INTO channel_connections (id, business_id, channel, provider, external_account_id, display_name)
           VALUES (:id, :b, CAST(:ch AS channel_type), CAST(:p AS channel_provider), :e, 'x')""",
        id=cid,
        b=t.business_id,
        ch=channel,
        p=provider,
        e=f"x-{cid}",
    )
    await set_(
        db,
        "UPDATE conversations SET channel_connection_id = :c, channel = CAST(:ch AS channel_type) WHERE id = :id",
        c=cid,
        ch=channel,
        id=t.conversation_id,
    )


async def test_allowed_by_default_and_blocked_by_the_kill_switch(db: Database) -> None:
    t = await make_tenant(db, "Policy")
    assert (await decide(db, t)).action == "allow"
    await set_(db, "UPDATE businesses SET kill_switch = true WHERE id = :b", b=t.business_id)
    clear_caches()
    assert (await decide(db, t)) == policy.PolicyDecision("block", "KILL_SWITCH_ACTIVE")


async def test_opted_out_customers_get_only_the_confirmation(db: Database) -> None:
    t = await make_tenant(db, "Opt")
    facts = {"facts": [{"key": "opted_out:web_widget", "value": True}]}
    await set_(
        db, "UPDATE customers SET memory = CAST(:m AS jsonb) WHERE id = :c", m=json.dumps(facts), c=t.customer_id
    )
    assert (await decide(db, t)).code == "OPTED_OUT"
    assert (await decide(db, t, origin="opt_confirm")).action == "allow"


async def test_a_person_holding_the_chat_blocks_the_ai_but_not_the_team(db: Database) -> None:
    t = await make_tenant(db, "Human")
    await set_(
        db,
        "UPDATE conversations SET human_active_until = now() + interval '10 minutes' WHERE id = :c",
        c=t.conversation_id,
    )
    assert (await decide(db, t)).code == "HUMAN_ACTIVE"
    assert (await decide(db, t, origin="team")).action == "allow"


async def test_the_24_hour_window(db: Database) -> None:
    wa = await make_tenant(db, "WA")
    await on_provider(db, wa, "meta_whatsapp", "whatsapp")
    await set_(db, "UPDATE conversations SET last_inbound_at = now() - interval '25 hours' WHERE id = :c",
               c=wa.conversation_id)  # fmt: skip
    closed = await decide(db, wa)
    assert closed.action == "template" and closed.template["name"] == get_settings().wa_order_update_template
    await set_(db, "UPDATE conversations SET last_inbound_at = now() - interval '1 hour' WHERE id = :c",
               c=wa.conversation_id)  # fmt: skip
    assert (await decide(db, wa)).action == "allow"

    page = await make_tenant(db, "Page")
    await on_provider(db, page, "meta_pages", "facebook_messenger")  # no template exists outside WhatsApp
    assert (await decide(db, page)).code == "WINDOW_CLOSED"  # never wrote in: the window never opened


async def test_automation_daily_cap(db: Database) -> None:
    t = await make_tenant(db, "Cap")
    for i in range(get_settings().automation_daily_cap):
        await make_outbound(db, t, key=f"auto:{i}:{new_id()}")
    assert (await decide(db, t, origin="automation")).code == "AUTOMATION_CAP_REACHED"
    assert (await decide(db, t, origin="ai_turn")).action == "allow"  # the cap is for automations only


@pytest.mark.parametrize(
    ("utc", "deferred_to"),
    [
        (datetime(2026, 10, 20, 17, 0, tzinfo=UTC), datetime(2026, 10, 21, 9, 0)),  # 22:30 IST: next morning
        (datetime(2026, 10, 20, 1, 0, tzinfo=UTC), datetime(2026, 10, 20, 9, 0)),  # 06:30 IST: the same morning
    ],
)
async def test_quiet_hours_defer_automations(
    db: Database, utc: datetime, deferred_to: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REFLEXO_DEMO_TIME_SCALE", "1.0")  # quiet hours apply in real time only
    get_settings.cache_clear()
    t = await make_tenant(db, "Quiet")
    set_clock(FixedClock(utc))
    d = await decide(db, t, origin="automation")
    assert d.action == "defer" and d.code == "QUIET_HOURS"
    assert d.defer_until is not None and d.defer_until.replace(tzinfo=None) == deferred_to
    assert (await decide(db, t, origin="ai_turn")).action == "allow"  # a reply to the customer is never held
    set_clock(FixedClock(utc + timedelta(hours=11)))  # 09:30 or 17:30 IST
    assert (await decide(db, t, origin="automation")).action == "allow"
    monkeypatch.undo()
    get_settings.cache_clear()
