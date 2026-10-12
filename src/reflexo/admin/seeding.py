"""Seed loading and demo reset (POC_02 §7). Used by `tasks.py seed|demo-reset` and the admin reset button.

Modes:
* onboarding — Meera's Bakes in `onboarding` with an empty catalogue (the demo builds it from a menu photo).
* full       — Meera's Bakes `active` with the full catalogue (rehearse later segments in isolation).
Kurti Kraft and eval-bakes are always loaded in full.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.knowledge.api as knowledge
from reflexo.common.config import Settings, get_settings
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger

log = get_logger(__name__)

SEED_DIR = Path(__file__).resolve().parents[3] / "config" / "seeds"
Mode = Literal["onboarding", "full"]
DEMO_SLUGS = ("meeras-bakes", "kurti-kraft", "eval-bakes")
TERMINATE_BUDGET_S = 20.0

OWNER_SCOPES = [
    "knowledge.write",
    "catalog.write",
    "conversations.read",
    "conversations.reply",
    "approvals.decide",
    "questions.answer",
    "automations.manage",
    "orders.manage",
    "team.manage",
    "settings.manage",
    "reports.read",
]


@dataclass
class SeedReport:
    businesses: dict[str, UUID] = field(default_factory=dict)
    seconds: float = 0.0
    workflows_terminated: int = 0
    workflows_unreachable: bool = False  # Temporal did not answer: live workflows of the old data may linger


def read_seed(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((SEED_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return data


def eval_seed(meera: dict[str, Any]) -> dict[str, Any]:
    """eval-bakes: a copy of Meera's full seed with a simulator connection only (POC_08 §4)."""
    seed = copy.deepcopy(meera)
    seed["slug"] = "eval-bakes"
    seed["name"] = "Eval Bakes"
    seed["order_number_prefix"] = "EB"
    # the eval owner talks through the simulator channel (owner_commands suite); simulated_only like the Lab's
    seed["owner"] = {"display_name": "Eval Owner", "simulated_whatsapp": "+919999900003", "simulator_id": "eval-owner"}
    seed["connections"] = [
        {
            "channel": "web_widget",
            "provider": "simulator",
            "external_account_id": "sim-eval-bakes",
            "display_name": "Eval Bakes (Simulator)",
            "roles": ["customer", "owner"],
        }
    ]
    return seed


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


async def _exec(s: AsyncSession, sql: str, **params: Any) -> None:
    await s.execute(text(sql), params)


async def _load_business(s: AsyncSession, seed: dict[str, Any], *, full: bool, settings: Settings) -> UUID:
    bid = new_id()
    status = "active" if full else "onboarding"
    await _exec(
        s,
        """INSERT INTO businesses (id, name, slug, default_language, status, settings)
                      VALUES (:id, :name, :slug, :lang, CAST(:status AS business_status), CAST(:settings AS jsonb))""",
        id=bid,
        name=seed["name"],
        slug=seed["slug"],
        lang=seed.get("default_language", "hi-Latn"),
        status=status,
        settings=_j(seed.get("settings", {}) | {"order_number_prefix": seed["order_number_prefix"]}),
    )
    await _exec(
        s,
        "INSERT INTO business_profiles (business_id, version, profile) VALUES (:b, 1, CAST(:p AS jsonb))",
        b=bid,
        p=_j(seed["profile"]),
    )
    await _exec(
        s,
        "INSERT INTO business_counters (business_id, name, value) VALUES (:b, 'order_number', :v)",
        b=bid,
        v=int(seed.get("order_number_start", 1000)),
    )

    # Owner + verified bindings
    owner = seed["owner"]
    member_id = new_id()
    owner_phone = getattr(settings, owner["whatsapp_setting"]) if owner.get("whatsapp_setting") else None
    await _exec(
        s,
        """INSERT INTO team_members (id, business_id, display_name, role, scopes, phone_e164)
                      VALUES (:id, :b, :n, 'owner', :scopes, :phone)""",
        id=member_id,
        b=bid,
        n=owner["display_name"],
        scopes=OWNER_SCOPES,
        phone=owner_phone,
    )
    bindings: list[tuple[str, str, bool]] = []
    if owner_phone:
        bindings.append(("whatsapp", owner_phone, False))
    if owner.get("telegram_setting"):
        bindings.append(("telegram", getattr(settings, owner["telegram_setting"]), False))
    if owner.get("simulated_whatsapp"):
        bindings.append(("whatsapp", owner["simulated_whatsapp"], True))
    if owner.get("simulator_id"):
        bindings.append(("web_widget", owner["simulator_id"], True))
    for i, (channel, external_id, simulated) in enumerate(bindings):
        await _exec(
            s,
            """INSERT INTO owner_channel_bindings
                            (id, business_id, member_id, channel, external_id, verified_at, is_primary, simulated_only)
                          VALUES (:id, :b, :m, CAST(:c AS channel_type), :e, now(), :primary, :sim)""",
            id=new_id(),
            b=bid,
            m=member_id,
            c=channel,
            e=external_id,
            primary=(i == 0),
            sim=simulated,
        )

    # Channel connections
    for conn in seed["connections"]:
        external = conn.get("external_account_id") or getattr(settings, conn["external_account_setting"])
        await _exec(
            s,
            """INSERT INTO channel_connections
               (id, business_id, channel, provider, external_account_id, display_name, roles, credential_ref)
                          VALUES (:id, :b, CAST(:c AS channel_type), CAST(:p AS channel_provider), :e, :n,
                                  CAST(:roles AS participant_role[]), :cred)""",
            id=new_id(),
            b=bid,
            c=conn["channel"],
            p=conn["provider"],
            e=external,
            n=conn["display_name"],
            roles=list(conn["roles"]),
            cred=conn.get("credential_ref"),
        )

    for schema in seed.get("order_detail_schemas", []):
        await _exec(
            s,
            """INSERT INTO order_detail_schemas (id, business_id, name, applies_to, fields)
                          VALUES (:id, :b, :n, CAST(:a AS jsonb), CAST(:f AS jsonb))""",
            id=new_id(),
            b=bid,
            n=schema["name"],
            a=_j(schema["applies_to"]),
            f=_j(schema["fields"]),
        )

    for qa in seed.get("qa", []):
        await _exec(
            s,
            """INSERT INTO qa_entries (id, business_id, question, question_variants, answer, topic, source)
                          VALUES (:id, :b, :q, :v, :a, CAST(:t AS topic), 'seed')""",
            id=new_id(),
            b=bid,
            q=qa["question"],
            v=qa.get("variants", []),
            a=qa["answer"],
            t=qa["topic"],
        )

    if full:
        for item in seed.get("catalog", []):
            oid = new_id()
            await _exec(
                s,
                """INSERT INTO offerings (id, business_id, kind, name, description, category, attributes,
                                                     base_price_paise, aliases, source)
               VALUES (:id, :b, 'product', :n, :d, :c, CAST(:attrs AS jsonb), :base, :aliases, 'seed')""",
                id=oid,
                b=bid,
                n=item["name"],
                d=item.get("description"),
                c=item.get("category"),
                attrs=_j(item.get("attributes", {})),
                base=item.get("base_price_paise"),
                aliases=item.get("aliases", []),
            )
            for v in item.get("variants", []):
                await _exec(
                    s,
                    """INSERT INTO offering_variants (id, business_id, offering_id, name, price_paise)
                                  VALUES (:id, :b, :o, :n, :p)""",
                    id=new_id(),
                    b=bid,
                    o=oid,
                    n=v["name"],
                    p=v["price_paise"],
                )

    await _exec(
        s,
        """INSERT INTO onboarding_sessions (id, business_id, state, completed_at)
                      VALUES (:id, :b, :st, :done)""",
        id=new_id(),
        b=bid,
        st="live" if full else "sources_requested",
        done=datetime.now(UTC) if full else None,
    )
    return bid


async def load(
    db: Database,
    mode: Mode = "onboarding",
    settings: Settings | None = None,
    slugs: tuple[str, ...] = DEMO_SLUGS,
) -> SeedReport:
    settings = settings or get_settings()
    started = time.perf_counter()
    report = SeedReport()
    meera = read_seed("meeras_bakes")
    seeds: list[tuple[dict[str, Any], bool]] = [
        (seed, full)
        for seed, full in ((meera, mode == "full"), (read_seed("kurti_kraft"), True), (eval_seed(meera), True))
        if seed["slug"] in slugs
    ]
    async with db.service_session(ServiceReason.SEED) as s:
        for seed, full in seeds:
            report.businesses[seed["slug"]] = await _load_business(s, seed, full=full, settings=settings)
    for business_id in report.businesses.values():
        await knowledge.reindex_business(business_id)  # search documents (text now, embeddings via job)
    report.seconds = time.perf_counter() - started
    log.info("seed_loaded", mode=mode, businesses=list(report.businesses), seconds=round(report.seconds, 2))
    return report


async def wipe(db: Database, slugs: tuple[str, ...] = DEMO_SLUGS) -> int:
    """Delete the demo businesses; FK cascades remove every dependent row."""
    async with db.service_session(ServiceReason.SEED) as s:
        result = await s.execute(text("DELETE FROM businesses WHERE slug = ANY(:slugs)"), {"slugs": list(slugs)})
        return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0


async def live_workflows(db: Database, slugs: tuple[str, ...] = DEMO_SLUGS) -> list[str]:
    """Workflows still running for the demo businesses: automation runs, imports, approvals that wait on a timer."""
    async with db.service_session(ServiceReason.SEED) as s:
        rows = (
            await s.execute(
                text("""
            WITH demo AS (SELECT id FROM businesses WHERE slug = ANY(:slugs))
            SELECT workflow_id FROM automation_runs
             WHERE business_id IN (SELECT id FROM demo) AND status IN ('running', 'waiting') AND workflow_id IS NOT NULL
            UNION
            SELECT workflow_id FROM import_runs
             WHERE business_id IN (SELECT id FROM demo) AND status IN ('queued', 'extracting', 'review')
               AND workflow_id IS NOT NULL
            UNION
            SELECT workflow_id FROM approval_requests
             WHERE business_id IN (SELECT id FROM demo) AND status = 'pending' AND workflow_id IS NOT NULL"""),
                {"slugs": list(slugs)},
            )
        ).all()
    return [r.workflow_id for r in rows]


async def _terminate(workflows: WorkflowPort, ids: list[str]) -> tuple[int, int]:
    """(terminated, failed). Best effort per workflow: one that cannot be stopped must not block the reset."""
    done = failed = 0
    for wf in ids:
        try:
            done += await workflows.terminate(wf, "demo reset")
        except Exception as exc:  # Temporal unreachable / RPC error: reported, the reset goes on
            failed += 1
            log.warning("demo_reset_terminate_failed", workflow_id=wf, error=type(exc).__name__)
    return done, failed


async def reset(
    db: Database,
    mode: Mode = "onboarding",
    settings: Settings | None = None,
    workflows: WorkflowPort | None = None,
    slugs: tuple[str, ...] = DEMO_SLUGS,
) -> SeedReport:
    """`demo-reset`: terminate the demo businesses' live workflows (their rows are about to go: a timer firing
    later would act on nothing, or retry forever), then wipe + reseed. Temporal being down does not block the
    reset — it is reported, and those workflows fail harmlessly when they wake."""
    started = time.perf_counter()
    ids = await live_workflows(db, slugs) if workflows else []
    terminated, unreachable = 0, False
    if ids and workflows:
        try:
            terminated, failed = await asyncio.wait_for(_terminate(workflows, ids), TERMINATE_BUDGET_S)
            unreachable = failed > 0
        except TimeoutError:
            unreachable = True
            log.warning("demo_reset_terminate_timeout", workflows=len(ids))
    deleted = await wipe(db, slugs)
    report = await load(db, mode, settings, slugs)
    report.workflows_terminated, report.workflows_unreachable = terminated, unreachable
    report.seconds = time.perf_counter() - started
    log.info(
        "demo_reset",
        mode=mode,
        deleted=deleted,
        workflows=len(ids),
        terminated=terminated,
        seconds=round(report.seconds, 2),
    )
    return report
