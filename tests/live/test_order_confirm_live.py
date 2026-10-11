"""Customer order confirmation with REAL Gemini.

Opt-in: `uv run pytest --live tests/live/test_order_confirm_live.py -s`.

Live regression (11 Oct 2026): after the read-back the customer said "ok thanks" and the agent replied "You're
welcome!" instead of confirming — the COD order stayed a draft, so no automation could run.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.commerce.api as commerce
import reflexo.gateway.api as gateway_api
import reflexo.integrations.api as integrations
import reflexo.knowledge.api as knowledge
from reflexo.agent.model_gateway.providers import GeminiProvider
from reflexo.common.config import Settings
from reflexo.common.contracts import OwnerNotifyPort, TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from tests.integration.test_customer_desk import Notes, make_bakery, outbound_texts, turn_for

pytestmark = [pytest.mark.live, pytest.mark.integration]


@pytest.fixture
def registry() -> Registry:
    key = Settings(_env_file=".env").google_api_key.get_secret_value()  # type: ignore[call-arg]
    if not key:
        pytest.skip("REFLEXO_GOOGLE_API_KEY not set")
    reg = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    agent.register(reg, provider=GeminiProvider(key))
    knowledge.register(reg)
    integrations.register(reg)
    commerce.register(reg)
    reg.provide(OwnerNotifyPort, Notes())  # type: ignore[type-abstract]
    return reg


async def say(registry: Registry, db: Database, pnid: str, wa: str, body: str) -> list[str]:
    handle = await turn_for(registry, db, pnid, wa, body)
    await registry.require(TurnRunnerPort).run_turn(handle)  # type: ignore[type-abstract]
    texts = await outbound_texts(db, handle.ref.conversation_id)
    print(f"\n> {body}\n< {texts[-1] if texts else None}")
    return texts


async def test_ok_thanks_after_read_back_confirms_the_cod_order(registry: Registry, db: Database) -> None:
    bid, pnid = await make_bakery(db)
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""UPDATE businesses SET settings = settings || '{"cod_rule": {"enabled": true,
                    "max_total_paise": 150000}}'::jsonb WHERE id = :b"""),
            {"b": bid},
        )
    wa = f"9198{new_id().int % 10**8:08d}"
    await say(
        registry, db, pnid, wa, "Pineapple cake 1 kg, eggless nahi, Kothrud 411038, kal shaam, COD se pay karunga"
    )
    await say(registry, db, pnid, wa, "ok thanks")
    async with db.service_session(ServiceReason.ADMIN) as s:
        rows: list[Any] = list(
            (
                await s.execute(
                    text("SELECT status, payment_method, payment_status FROM orders WHERE business_id = :b"),
                    {"b": bid},
                )
            ).all()
        )
    assert [(str(r.status), str(r.payment_method), str(r.payment_status)) for r in rows] == [
        ("confirmed", "cod", "cod_pending")
    ]
