"""decide() and the rule checker against the real database and the real checker image (BETA_04 §7.6, §7.7):
compare and lint before the read-back (CHK-POL-3, CHK-POL-4), a missing checker (CHK-POL-5), limits over history
(CHK-POL-6), and a decision record for every effect (CHK-ARCH-5)."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import reflexo.admin.api as admin
import reflexo.agent.api as agent
import reflexo.knowledge.api as knowledge
import reflexo.policy.api as policy
from reflexo.agent import rules
from reflexo.common.config import get_settings
from reflexo.common.contracts import TurnRunnerPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.ports import Registry
from reflexo.knowledge.playbook import Condition
from tests.integration import test_learning
from tests.integration.test_ask_owner import Bakery, bakery, owner_messages, owner_says, q, wa_number
from tests.integration.test_customer_desk import PRIMARY, tool_results, turn_for
from tests.integration.test_learning import SAY, asks_to_cancel, placed_order, status_of
from tests.integration.test_order_turns import call
from tests.integration.test_owner_desk import owner_turn, read_back_reply, steps

pytestmark = pytest.mark.integration
fake, registry = test_learning.fake, test_learning.registry


def capturing(results: list[dict[str, Any]]) -> Any:
    def script(a: agent.Attempt) -> agent.FakeReply:
        results.append(tool_results(a)[-1])
        last = results[-1]
        return read_back_reply(a) if "data" in last else say_error(str(last.get("error")))

    return script


def say_error(t: str) -> agent.FakeReply:
    from tests.integration.test_owner_desk import say_owner

    return say_owner(t)


async def teach(
    registry: Registry, db: Database, fake: agent.FakeProvider, shop: Bakery, **rule: Any
) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []
    fake.script(PRIMARY, steps(call("playbook_change", **rule), capturing(seen)))
    await owner_turn(registry, db, shop, str(rule.get("says")))
    return seen[0]


@pytest.mark.slow
@pytest.mark.check("CHK-POL-3")
async def test_a_widened_rule_is_read_back_with_what_it_newly_allows(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    await knowledge.playbook.add_rule(
        shop.bid, capability="order.cancel", effect="allow", source="owner_instruction",
        conditions=[Condition(field="order.total_rupees", op="lt", value=500)], says="chhote orders cancel kar do",
    )  # fmt: skip
    result = await teach(
        registry, db, fake, shop, capability="order.cancel", effect="allow", conditions=[], says="sab cancel kar do"
    )
    check = result["data"]["check"]
    assert check["status"] == "ok"
    [newly] = [d for d in check["differences"] if d["kind"] == "newly_acts"]
    assert newly["when"] == "the order total (₹) is 500"  # the roundest counterexample, in the owner's words
    read_back = str((await owner_messages(db, shop))[-1])
    assert "This also lets me do it myself when the order total (₹) is 500." in read_back


@pytest.mark.slow
@pytest.mark.check("CHK-POL-4")
async def test_a_rule_that_can_never_apply_is_not_saved(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    before = len(await q(db, "SELECT id FROM knowledge_change_sets WHERE business_id = :b", b=shop.bid))
    impossible = [
        {"field": "order.hours_until_delivery", "op": "lt", "value": 10},
        {"field": "order.hours_until_delivery", "op": "gt", "value": 20},
    ]
    result = await teach(
        registry, db, fake, shop, capability="order.cancel", effect="allow", conditions=impossible, says="..."
    )
    assert result["error"]["code"] == "RULE_NEVER_APPLIES"
    assert len(await q(db, "SELECT id FROM knowledge_change_sets WHERE business_id = :b", b=shop.bid)) == before
    assert not await q(db, "SELECT id FROM approval_requests WHERE business_id = :b", b=shop.bid)


@pytest.mark.check("CHK-POL-5")
async def test_with_the_checker_missing_a_rule_asks_first_until_checked(
    registry: Registry, db: Database, fake: agent.FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = await bakery(db)
    working = get_settings().policy_check_cmd
    monkeypatch.setenv("REFLEXO_POLICY_CHECK_CMD", "reflexo-policy-check-not-installed")
    get_settings.cache_clear()
    result = await teach(
        registry, db, fake, shop, capability="order.cancel", effect="allow", says="cancel kar do",
        conditions=[{"field": "order.hours_until_delivery", "op": "gte", "value": 24}],
    )  # fmt: skip
    data = result["data"]
    assert data["check"]["status"] == "unchecked" and "ask them first" in data["next"]  # the owner is told
    assert any("I ask you first" in line for line in data["read_back"])
    [job] = await q(db, "SELECT payload FROM jobs WHERE queue = :q", q=rules.RECHECK_QUEUE)
    assert job.payload["asked_effect"] == "allow"
    await rules.recheck(dict(job.payload))  # the owner has not said YES yet: try again in an hour
    waiting = await q(db, "SELECT payload FROM jobs WHERE queue = :q ORDER BY created_at", q=rules.RECHECK_QUEUE)
    assert [j.payload["tries"] for j in waiting] == [0, 1]
    read_back = str((await owner_messages(db, shop))[-1])
    await owner_says(registry, db, shop, f"YES {re.search(r'YES (\d{4})', read_back).group(1)}")  # type: ignore[union-attr]
    [rule] = await knowledge.playbook.rules(shop.bid)
    assert rule.effect == "propose"  # saved as "ask me first", whatever was asked

    async def stored() -> dict[str, Any]:  # what the Rules page shows (playbook_rules.check_result)
        [row] = await q(db, "SELECT check_result FROM playbook_rules WHERE id = :r", r=rule.id)
        return dict(row.check_result)

    first = await stored()
    assert (first["status"], first["asked_effect"], first["tries"]) == ("unchecked", "allow", 0)
    await rules.recheck(dict(job.payload) | {"tries": 1})  # still missing an hour later: the try is counted
    assert ((await stored())["status"], (await stored())["tries"]) == ("unchecked", 2)
    [view] = (await admin.workspace.playbook(shop.bid))["rules"]
    assert view["check"]["status"] == "unchecked" and "I do it myself" in view["asked_readback"]

    monkeypatch.setenv("REFLEXO_POLICY_CHECK_CMD", working)  # the checker is back: the hourly re-check passes
    get_settings.cache_clear()
    await rules.recheck(dict(job.payload))
    assert (await stored())["status"] == "ok"  # checked; the rule still asks first until the owner confirms
    note = str((await owner_messages(db, shop))[-1])
    approval = re.search(r"apv:([0-9a-f-]{36}):a", note).group(1)  # type: ignore[union-attr]
    assert "I do it myself when hours left until delivery is at least 24" in note
    await owner_says(registry, db, shop, callback=f"apv:{approval}:a")
    [rule] = await knowledge.playbook.rules(shop.bid)
    assert rule.effect == "allow"
    [view] = (await admin.workspace.playbook(shop.bid))["rules"]
    assert view["check"]["status"] == "ok" and view["asked_readback"] is None  # saved as asked


async def _paid_order(db: Database, shop: Bakery, total_rupees: int) -> tuple[UUID, UUID]:
    """A paid order of this business (state set directly; the capability only reads the order's facts)."""
    async with db.service_session(ServiceReason.SEED) as s:
        customer = new_id()
        await s.execute(
            text("""INSERT INTO customers (id, business_id, display_name, phone_e164)
                    VALUES (:c, :b, 'Asha', :p)"""),
            {"c": customer, "b": shop.bid, "p": f"+91{new_id().int % 10**10:010d}"},
        )
        order = new_id()
        await s.execute(
            text("""INSERT INTO orders (id, business_id, customer_id, order_number, status, payment_method,
                                        payment_status, total_paise)
                    VALUES (:o, :b, :c, :n, 'confirmed', 'razorpay_link', 'paid', :t)"""),
            {
                "o": order,
                "b": shop.bid,
                "c": customer,
                "n": f"MB-{new_id().int % 9000 + 1000}",
                "t": total_rupees * 100,
            },
        )
    return order, customer


async def _refunded_today(db: Database, shop: Bakery, total_rupees: int) -> None:
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("""INSERT INTO domain_events (id, business_id, type, source, correlation_id, payload)
                    VALUES (:i, :b, 'payment.refunded', 'commerce', :i, CAST(:p AS jsonb))"""),
            {"i": new_id(), "b": shop.bid, "p": json.dumps({"total_paise": total_rupees * 100})},
        )


@pytest.mark.check("CHK-POL-6")
async def test_a_daily_refund_limit_proposes_the_refund_that_crosses_it(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    await knowledge.playbook.add_rule(
        shop.bid, capability="refund.request", effect="allow", conditions=[], says="refund kar do",
        source="owner_instruction",
    )  # fmt: skip
    await knowledge.playbook.add_rule(
        shop.bid, capability="refund.request", effect="propose", source="owner_instruction",
        conditions=[Condition(field="refunds.today_rupees", op="gt", value=5000)],
        says="ek din mein ₹5,000 se zyada refund ho to mujhse poochna",
    )  # fmt: skip
    await _refunded_today(db, shop, 3000)

    async def decision(total: int) -> policy.Decision:
        order, customer = await _paid_order(db, shop, total)
        return await agent.decide(
            agent.ActionRequest(
                capability="refund.request", business_id=shop.bid, requested_by="automation", desk="automation",
                customer_id=customer, resource=policy.EntityRef(type="Order", id=str(order)),
            )
        )  # fmt: skip

    under = await decision(1500)  # 3,000 + 1,500 = 4,500 today
    assert (under.effect, under.says) == ("act", "refund kar do")
    over = await decision(2500)  # 3,000 + 2,500 = 5,500: crosses the owner's limit
    assert (over.effect, over.layer) == ("propose", "shop")
    assert over.says == "ek din mein ₹5,000 se zyada refund ho to mujhse poochna"


# ---------------------------------------------------------------- every effect has a decision (CHK-ARCH-5)

EFFECTS = {"order.cancelled": "order.cancel", "order.changed": "order.change", "payment.refunded": "refund.request"}


async def undecided_effects(db: Database, business_id: UUID) -> list[str]:
    """Effects of this business done by a person or the AI, each matched to an earlier `act` decision for its
    capability. Automation sends are keyed `auto:` by the runtime."""
    effects = [
        (EFFECTS[r.type], r.occurred_at)
        for r in await q(
            db,
            """SELECT type, occurred_at FROM domain_events WHERE business_id = :b AND type = ANY(:t)
                 AND payload->>'by' IN ('customer', 'owner')""",
            b=business_id,
            t=list(EFFECTS),
        )
    ]
    effects += [
        ("message.send", r.created_at)
        for r in await q(
            db,
            """SELECT created_at FROM outbound_messages
               WHERE business_id = :b AND idempotency_key LIKE 'auto:%' AND part_index = 0""",
            b=business_id,
        )
    ]
    decisions = [
        (r.attrs["capability"], r.created_at)
        for r in await q(
            db,
            """SELECT attrs, created_at FROM pipeline_events WHERE business_id = :b AND stage = 'decision'
                 AND attrs->>'effect' = 'act' ORDER BY created_at""",
            b=business_id,
        )
    ]
    missing = []
    for capability, at in sorted(effects, key=lambda e: e[1]):
        match = next((d for d in decisions if d[0] == capability and d[1] <= at), None)
        if match is None:
            missing.append(f"{capability} at {at}")
        else:
            decisions.remove(match)
    return missing


@pytest.mark.check("CHK-ARCH-5")
async def test_every_effect_of_a_run_has_a_decision_record(
    registry: Registry, db: Database, fake: agent.FakeProvider
) -> None:
    shop = await bakery(db)
    wa = wa_number()
    order, _conv = await placed_order(registry, db, fake, shop, wa)
    async with db.service_session(ServiceReason.ADMIN) as s:  # a draft: the customer's cancel acts by default
        await s.execute(text("UPDATE orders SET status = 'draft' WHERE id = :o"), {"o": order.order_id})
    fake.script(PRIMARY, asks_to_cancel(order.order_id, SAY))
    await registry.require(TurnRunnerPort).run_turn(await turn_for(registry, db, shop.pnid, wa, "cancel it"))  # type: ignore[type-abstract]
    assert await status_of(db, order.order_id) == "cancelled"

    second, _ = await placed_order(registry, db, fake, shop, wa_number())  # the owner cancels: read-back, YES
    fake.script(
        PRIMARY, steps(call("order_action", order_number=second.order_number, action="cancel"), read_back_reply)
    )
    await owner_turn(registry, db, shop, f"{second.order_number} cancel kar do")
    read_back = str((await owner_messages(db, shop))[-1])
    await owner_says(registry, db, shop, f"YES {re.search(r'YES (\d{4})', read_back).group(1)}")  # type: ignore[union-attr]
    assert await status_of(db, second.order_id) == "cancelled"

    assert await undecided_effects(db, shop.bid) == []
    decided = await q(db, "SELECT attrs FROM pipeline_events WHERE business_id = :b AND stage = 'decision'", b=shop.bid)
    assert {d.attrs["requested_by"] for d in decided} >= {"customer_turn", "owner_turn"}
