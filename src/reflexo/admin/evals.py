"""Eval suites (POC_08 §4, F18): the demo build measured with REAL Gemini on the dedicated `eval-bakes` business.

Two ways a case runs:

* **Through the deployed pipeline** (`tool_selection`, `grounding`, `language`, `owner_commands`): a signed simulator
  envelope enters the real ingress, the running worker takes the turn exactly as in production, and the outcome is
  read back from the X-ray (`pipeline_events`: which tools ran, the reply's action, language, referenced record
  types, gate repairs) and the stored reply. Nothing is mocked — the score is the build's.
* **In-process on the real code path** (`gate_adversarial`, `ask_owner_scope`, `builder`, `learning`): the gate on
  forced replies hydrated against eval-bakes' data; the ask-owner scope decider; the automation builder with the
  same validate-and-repair loop as its workflow; the learning loop's generalisation of an owner's decision.

Every run starts by reseeding eval-bakes only, so runs are comparable and the demo businesses are untouched.
Datasets: `evals/datasets/<suite>.yaml` (hand-written, versioned; every rehearsal bug becomes a case).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from sqlalchemy import text

import reflexo.agent.api as agent
import reflexo.automations.api as automations
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
from reflexo.admin import seeding
from reflexo.common import ports
from reflexo.common.config import get_settings
from reflexo.common.contracts import WorkflowPort
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[3] / "evals"
DATASETS = ROOT / "datasets"
REPORTS = ROOT / "reports"
EVAL_SLUG = "eval-bakes"
TURN_TIMEOUT_S = 120.0
PIPELINE_CONCURRENCY = 4
BUILDER_REPAIRS = 2  # as the build workflow (automation_build.MAX_REPAIRS)

# suite → (pass threshold, how it runs). Thresholds from POC_08 §4.2; `learning` is ADR-015's addition.
THRESHOLDS: dict[str, float] = {
    "tool_selection": 0.85,
    "gate_adversarial": 1.0,
    "grounding": 0.90,
    "language": 0.90,
    "ask_owner_scope": 0.80,
    "owner_commands": 0.85,
    "builder": 7 / 8,
    "learning": 0.80,
}
ORDER = list(THRESHOLDS)  # in-process suites interleave fine; owner_commands mutates eval data, so it runs last


@dataclass
class CaseResult:
    id: str
    passed: bool
    detail: str
    seconds: float
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    suite: str
    cases: int
    passed: int
    threshold: float
    metrics: dict[str, Any]
    results: list[CaseResult]

    @property
    def score(self) -> float:
        return self.passed / self.cases if self.cases else 0.0

    @property
    def ok(self) -> bool:
        return self.cases > 0 and self.score >= self.threshold


def load_cases(suite: str) -> list[dict[str, Any]]:
    data = yaml.safe_load((DATASETS / f"{suite}.yaml").read_text(encoding="utf-8")) or {}
    cases: list[dict[str, Any]] = data.get("cases", [])
    return cases


def build_ref() -> str:
    """The deployed git sha (REFLEXO_BUILD_REF in images without git), else the working tree's."""
    if ref := os.environ.get("REFLEXO_BUILD_REF"):
        return ref
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - fixed command, no user input
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT.parent,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT.parent,
            timeout=5,
        ).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ---------------------------------------------------------------- the eval business


@dataclass
class EvalShop:
    business_id: UUID
    connection_id: UUID
    owner_member_id: UUID


async def prepare(reseed: bool = True) -> EvalShop:
    if reseed:
        await seeding.reset(
            get_db(),
            "full",
            workflows=ports.registry.optional(WorkflowPort),  # type: ignore[type-abstract]
            slugs=(EVAL_SLUG,),
        )
        gateway.clear_connection_cache()
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        row = (
            await s.execute(
                text("""SELECT b.id AS bid, c.id AS cid, m.id AS mid FROM businesses b
                        JOIN channel_connections c ON c.business_id = b.id AND c.provider = 'simulator'
                        JOIN team_members m ON m.business_id = b.id AND m.role = 'owner'
                        WHERE b.slug = :slug LIMIT 1"""),
                {"slug": EVAL_SLUG},
            )
        ).first()
    if row is None:
        raise RuntimeError("eval-bakes is not seeded (run `make demo-reset` once)")
    return EvalShop(row.bid, row.cid, row.mid)


async def _owner_identity(shop: EvalShop, sender: str) -> None:
    """A fresh owner thread per owner case: a simulated-only binding for this sender on the simulator channel."""
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO owner_channel_bindings
                        (id, business_id, member_id, channel, external_id, verified_at, is_primary, simulated_only)
                    VALUES (:id, :b, :m, 'web_widget', :e, now(), false, true)
                    ON CONFLICT DO NOTHING"""),
            {"id": new_id(), "b": shop.business_id, "m": shop.owner_member_id, "e": sender},
        )


async def _workers_alive() -> bool:
    async with get_db().service_session(ServiceReason.HEALTH) as s:
        n = (
            await s.execute(
                text("""SELECT count(*) FROM worker_heartbeats
                        WHERE beat_at > now() - interval '30 seconds' AND 'turns' = ANY(pools)""")
            )
        ).scalar_one()
    return bool(n)


# ---------------------------------------------------------------- driving the real pipeline


@dataclass
class Observation:
    turn_id: str | None = None
    tools: list[str] = field(default_factory=list)
    action: str | None = None
    topic: str | None = None
    language: str | None = None
    refs: list[str] = field(default_factory=list)
    reply: str = ""
    gate_repairs: int = 0
    model_calls: int = 0
    seconds: float | None = None
    error: str | None = None


async def say(shop: EvalShop, sender: str, body: str, name: str = "Eval Customer") -> Observation:
    """One message from `sender` through the signed simulator ingress; waits for the turn's reply."""
    event_id = f"eval-{new_id().hex}"
    raw = json.dumps({"event_id": event_id, "sender": {"id": sender, "name": name}, "text": body}).encode()
    headers = {
        "x-reflexo-signature": gateway.sign_simulator(raw),
        "x-reflexo-simulated": "1",
        "content-type": "application/json",
    }
    sent = time.perf_counter()
    result = await gateway.ingest_webhook("simulator", headers, raw, {"connection_id": str(shop.connection_id)})
    if result.status_code != 200:
        return Observation(error=f"ingest {result.status_code}")
    return await _await_reply(shop, event_id, sent)


async def _await_reply(shop: EvalShop, event_id: str, sent: float) -> Observation:
    deadline = time.monotonic() + TURN_TIMEOUT_S
    turn_id: UUID | None = None
    while time.monotonic() < deadline:
        async with get_db().service_session(ServiceReason.ADMIN) as s:
            if turn_id is None:
                turn_id = (
                    await s.execute(
                        text("""SELECT turn_id FROM messages
                                WHERE channel_connection_id = :c AND external_message_id = :e"""),
                        {"c": shop.connection_id, "e": event_id},
                    )
                ).scalar_one_or_none()
            if turn_id is not None:
                events = (
                    await s.execute(
                        text("""SELECT stage, status, attrs FROM pipeline_events
                                WHERE turn_id = :t ORDER BY created_at"""),
                        {"t": turn_id},
                    )
                ).all()
                reply = next((e for e in events if e.stage == "reply"), None)
                if reply is not None:
                    texts = (
                        await s.execute(
                            text("""SELECT content_text, content_blocks FROM messages
                                    WHERE turn_id = :t AND direction = 'outbound' ORDER BY created_at"""),
                            {"t": turn_id},
                        )
                    ).all()
                    a = reply.attrs or {}
                    return Observation(
                        turn_id=str(turn_id),
                        tools=[str((e.attrs or {}).get("tool")) for e in events if e.stage == "tool_call"],
                        action=a.get("action"),
                        topic=a.get("topic"),
                        language=a.get("language"),
                        refs=list(a.get("refs") or []),
                        reply="\n".join(_own_words(t.content_blocks, t.content_text) for t in texts),
                        gate_repairs=sum(1 for e in events if e.stage == "gate" and str(e.status) == "error"),
                        model_calls=int(a.get("model_calls") or 0),
                        seconds=round(time.perf_counter() - sent, 2),
                    )
        await asyncio.sleep(0.5)
    return Observation(turn_id=str(turn_id) if turn_id else None, error="timeout: no reply")


def _own_words(blocks: list[dict[str, Any]] | None, fallback: str | None) -> str:
    """What the model wrote (text, button and list texts) — not the cards rendered from catalogue rows, whose
    names stay in the shop's own language."""
    if not blocks:
        return fallback or ""
    return "\n".join(str(b["text"]) for b in blocks if b.get("type") in ("text", "buttons", "list") and b.get("text"))


async def conversation(shop: EvalShop, sender: str, messages: list[str]) -> Observation:
    """Earlier messages set the context; the last one is scored."""
    obs = Observation(error="no messages")
    for body in messages:
        obs = await say(shop, sender, body)
        if obs.error:
            return obs
    return obs


# ---------------------------------------------------------------- scoring helpers

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")


def script_matches(reply: str, lang: str) -> bool:
    """The reply is written in the expected script: Devanagari for `hi`, Latin for `hi-Latn` and `en`."""
    deva, latin = len(_DEVANAGARI.findall(reply)), len(_LATIN.findall(reply))
    return deva > latin if lang == "hi" else latin > deva


def _tools_verdict(obs: Observation, case: dict[str, Any]) -> tuple[bool, str]:
    called = set(obs.tools)
    missing = [t for t in case.get("expect_tools", []) if t not in called]
    any_of = case.get("expect_any_tool", [])
    forbidden = [t for t in case.get("forbid_tools", []) if t in called]
    problems = []
    if missing:
        problems.append(f"missing {missing}")
    # `or_action`: calling one of `expect_any_tool` is right, and so is the alternative outcome (e.g. a search
    # for an unknown product, or asking the owner directly)
    if any_of and not called & set(any_of) and obs.action not in case.get("or_action", []):
        problems.append(f"none of {any_of}")
    if forbidden:
        problems.append(f"forbidden {forbidden}")
    if case.get("expect_action") and obs.action != case["expect_action"]:
        problems.append(f"action {obs.action} ≠ {case['expect_action']}")
    if case.get("expect_action_any") and obs.action not in case["expect_action_any"]:
        problems.append(f"action {obs.action} not in {case['expect_action_any']}")
    return not problems, "; ".join(problems) or "ok"


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 2)


def _pipeline_metrics(results: list[CaseResult]) -> dict[str, Any]:
    secs = [float(r.observed["seconds"]) for r in results if r.observed.get("seconds") is not None]
    calls = [int(r.observed.get("model_calls") or 0) for r in results if r.observed.get("turn_id")]
    return {
        "p50_s": _pct(secs, 0.5),
        "p95_s": _pct(secs, 0.95),
        "one_call_turns": round(sum(1 for c in calls if c <= 1) / len(calls), 2) if calls else None,
        "gate_repairs": sum(int(r.observed.get("gate_repairs") or 0) for r in results),
        "errors": sum(1 for r in results if r.observed.get("error")),
    }


async def _bounded(items: list[Any], fn: Callable[[Any], Awaitable[CaseResult]], limit: int) -> list[CaseResult]:
    gate = asyncio.Semaphore(limit)

    async def one(item: Any) -> CaseResult:
        async with gate:
            started = time.perf_counter()
            try:
                return await fn(item)
            except Exception as exc:  # a broken case is a failed case, never a crashed run
                log.exception("eval_case_crashed", case=item.get("id"))
                return CaseResult(
                    item.get("id", "?"),
                    False,
                    f"crashed: {type(exc).__name__}: {exc}"[:300],
                    round(time.perf_counter() - started, 2),
                )

    return list(await asyncio.gather(*(one(i) for i in items)))


def _obs(obs: Observation) -> dict[str, Any]:
    return asdict(obs)


# ---------------------------------------------------------------- suites through the pipeline


async def suite_tool_selection(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        obs = await conversation(shop, f"eval-{run}-{c['id']}", c["messages"])
        if obs.error:
            return CaseResult(c["id"], False, obs.error, obs.seconds or 0, _obs(obs))
        ok, why = _tools_verdict(obs, c)
        return CaseResult(c["id"], ok, f"{why} · tools {obs.tools}", obs.seconds or 0, _obs(obs))

    return await _bounded(cases, one, PIPELINE_CONCURRENCY)


async def suite_grounding(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        obs = await conversation(shop, f"eval-{run}-{c['id']}", c["messages"])
        if obs.error:
            return CaseResult(c["id"], False, obs.error, obs.seconds or 0, _obs(obs))
        problems = []
        if obs.action != c.get("expect_action", "send"):
            problems.append(f"action {obs.action}")
        missing = [t for t in c.get("must_reference", []) if t not in obs.refs]
        if missing:
            problems.append(f"refs {obs.refs} lack {missing}")
        any_ref = c.get("must_reference_any", [])
        if any_ref and not set(any_ref) & set(obs.refs):
            problems.append(f"refs {obs.refs} lack any of {any_ref}")
        wanted = c.get("reply_contains_any", [])
        if wanted and not any(w.lower() in obs.reply.lower() for w in wanted):
            problems.append(f"reply lacks any of {wanted}")
        return CaseResult(c["id"], not problems, "; ".join(problems) or "ok", obs.seconds or 0, _obs(obs))

    return await _bounded(cases, one, PIPELINE_CONCURRENCY)


async def suite_language(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        obs = await conversation(shop, f"eval-{run}-{c['id']}", [c["message"]])
        if obs.error:
            return CaseResult(c["id"], False, obs.error, obs.seconds or 0, _obs(obs))
        # English-only platform: every reply is English whatever the customer wrote; "auto": mirror the customer
        policy = get_settings().language_policy
        want = c["expected_lang"] if policy == "auto" else policy
        words_ok = want != "en" or agent.detect_language(obs.reply, "en") == "en"  # no Hinglish in an English reply
        ok = obs.language == want and script_matches(obs.reply, want) and words_ok
        why = f"want {want}: declared {obs.language}, script {'ok' if script_matches(obs.reply, want) else 'wrong'}" + (
            "" if words_ok else ", Hinglish words"
        )
        return CaseResult(c["id"], ok, why, obs.seconds or 0, _obs(obs))

    return await _bounded(cases, one, PIPELINE_CONCURRENCY)


async def suite_owner_commands(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        sender = f"eval-owner-{run}-{c['id']}"
        await _owner_identity(shop, sender)
        obs = await conversation(shop, sender, c["messages"])
        if obs.error:
            return CaseResult(c["id"], False, obs.error, obs.seconds or 0, _obs(obs))
        ok, why = _tools_verdict(obs, c)
        return CaseResult(c["id"], ok, f"{why} · tools {obs.tools}", obs.seconds or 0, _obs(obs))

    return await _bounded(cases, one, 2)  # owner changes touch shared catalogue rows: keep it gentle


# ---------------------------------------------------------------- in-process suites


async def _resolve_refs(shop: EvalShop, refs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Dataset refs by name → record ids in eval-bakes, e.g. `offering: Red Velvet Cake` or
    `variant: Red Velvet Cake / 1 kg`."""
    out = []
    for r in refs:
        kind, name = next(iter(r.items()))
        if kind == "offering_raw":  # an id that exists nowhere (the references check)
            out.append({"type": "offering", "id": name})
        elif kind in ("offering", "variant"):
            offering, _, variant = name.partition(" / ")
            cards = await knowledge.search_catalog(shop.business_id, offering, k=10)
            card = next((c for c in cards if c.name == offering), None)
            if card is None:
                raise LookupError(f"no offering {offering!r} in eval-bakes")
            if kind == "offering":
                out.append({"type": "offering", "id": str(card.offering_id)})
            else:
                v = next(v for v in card.variants if v.name == variant)
                out.append({"type": "variant", "id": str(v.variant_id)})
        else:
            out.append({"type": kind, "id": name})
    return out


async def suite_gate_adversarial(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        started = time.perf_counter()
        output = {
            "action": c.get("action", "send"),
            "blocks": [{"type": "text", "text": c["text"]}, *c.get("extra_blocks", [])],
            "referenced_records": await _resolve_refs(shop, c.get("refs", [])),
            "topic": c.get("topic", "pricing"),
            "language": c.get("language", "en"),
        }
        passed, failed = await agent.gate_check(shop.business_id, output)
        blocked = not passed
        expect_block = c.get("expect_block", True)
        ok = blocked == expect_block and (not c.get("check") or c["check"] in failed or not expect_block)
        why = f"{'blocked' if blocked else 'passed'} {failed}" if failed else ("blocked" if blocked else "passed")
        return CaseResult(c["id"], ok, why, round(time.perf_counter() - started, 3), {"failed_checks": failed})

    return await _bounded(cases, one, 8)


async def suite_ask_owner_scope(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        started = time.perf_counter()
        scope = await agent.decide_answer_scope(shop.business_id, c["question"], c["answer"])
        ok = scope == c["expected_scope"]
        return CaseResult(c["id"], ok, f"{scope} (want {c['expected_scope']})", round(time.perf_counter() - started, 2))

    return await _bounded(cases, one, 4)


async def suite_builder(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        started = time.perf_counter()
        drafts: list[dict[str, Any]] = []
        problems: list[str] = []
        for _ in range(BUILDER_REPAIRS + 1):
            drafts = await automations.builder.draft(
                shop.business_id, c["description"], feedback=problems or None, previous=drafts or None
            )
            drafts, problems = automations.builder.check(drafts)
            if not problems:
                break
        failures = [f"invalid: {problems}"] if problems else []
        failures += _builder_assertions(drafts, c.get("expect", {}))
        observed = {"definitions": drafts}
        why = "; ".join(failures) or f"ok ({len(drafts)} definitions)"
        return CaseResult(c["id"], not failures, why, round(time.perf_counter() - started, 2), observed)

    return await _bounded(cases, one, 3)


def _builder_assertions(drafts: list[dict[str, Any]], expect: dict[str, Any]) -> list[str]:
    out = []
    if "count" in expect and len(drafts) != expect["count"]:
        out.append(f"{len(drafts)} definitions ≠ {expect['count']}")
    triggers = {str((d.get("trigger") or {}).get("type")) for d in drafts}
    for t in expect.get("triggers", []):
        if t not in triggers:
            out.append(f"no {t} trigger (have {sorted(triggers)})")
    steps = [s for d in drafts for s in d.get("steps", [])]
    types = {str(s.get("type")) for s in steps}
    for t in expect.get("step_types", []):
        if t not in types:
            out.append(f"no {t} step")
    if "counter_limit" in expect:
        limits = [s.get("limit") for s in steps if s.get("type") == "counter_limit"]
        if expect["counter_limit"] not in limits:
            out.append(f"limit {limits} ≠ {expect['counter_limit']}")
    blob = json.dumps(drafts, ensure_ascii=False).lower()
    for needle in expect.get("mentions", []):
        if needle.lower() not in blob:
            out.append(f"no mention of {needle!r}")
    return out


async def suite_learning(shop: EvalShop, cases: list[dict[str, Any]], run: str) -> list[CaseResult]:
    async def one(c: dict[str, Any]) -> CaseResult:
        started = time.perf_counter()
        rule = await agent.generalise_decision(
            shop.business_id, c["capability"], c["situation"], c.get("reason", ""), c["approve"]
        )
        fields = [str(x["field"]) for x in rule["conditions"]]
        problems = []
        if rule["effect"] != ("allow" if c["approve"] else "deny"):
            problems.append(f"effect {rule['effect']}")
        if c.get("expect_any_field") and not set(fields) & set(c["expect_any_field"]):
            problems.append(f"none of {c['expect_any_field']} in {fields}")
        bad = [f for f in fields if f in c.get("forbid_fields", [])]
        if bad:
            problems.append(f"incidental {bad}")
        why = "; ".join(problems) or f"ok {rule['conditions']}"
        return CaseResult(c["id"], not problems, why, round(time.perf_counter() - started, 2), {"rule": rule})

    return await _bounded(cases, one, 4)


SuiteFn = Callable[[EvalShop, list[dict[str, Any]], str], Awaitable[list[CaseResult]]]
SUITES: dict[str, SuiteFn] = {
    "tool_selection": suite_tool_selection,
    "gate_adversarial": suite_gate_adversarial,
    "grounding": suite_grounding,
    "language": suite_language,
    "ask_owner_scope": suite_ask_owner_scope,
    "owner_commands": suite_owner_commands,
    "builder": suite_builder,
    "learning": suite_learning,
}
PIPELINE_SUITES = frozenset({"tool_selection", "grounding", "language", "owner_commands"})


# ---------------------------------------------------------------- run, store, report


async def run(
    suites: list[str] | None = None,
    *,
    reseed: bool = True,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, list[SuiteResult]]:
    names = [s for s in ORDER if s in (suites or ORDER)]
    unknown = set(suites or []) - set(SUITES)
    if unknown:
        raise ValueError(f"unknown suite(s): {sorted(unknown)}")
    if PIPELINE_SUITES & set(names) and not await _workers_alive():
        raise RuntimeError("no pipeline worker is running (start `make worker`): pipeline suites need it")
    ref = build_ref()
    shop = await prepare(reseed)
    run_id = new_id().hex[:8]
    out: list[SuiteResult] = []
    for name in names:
        cases = load_cases(name)
        say_ = progress or (lambda _m: None)
        say_(f"{name}: {len(cases)} cases …")
        started = time.perf_counter()
        results = await SUITES[name](shop, cases, run_id)
        passed = sum(r.passed for r in results)
        metrics: dict[str, Any] = {"seconds": round(time.perf_counter() - started, 1)}
        if name in PIPELINE_SUITES:
            metrics |= _pipeline_metrics(results)
        suite = SuiteResult(name, len(results), passed, THRESHOLDS[name], metrics, results)
        out.append(suite)
        await _store(ref, suite)
        say_(
            f"{name}: {passed}/{len(results)} ({suite.score:.0%}, threshold {suite.threshold:.0%}) "
            f"{'PASS' if suite.ok else 'FAIL'}"
        )
    write_report(ref, out)
    return ref, out


async def _store(ref: str, suite: SuiteResult) -> None:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        await s.execute(
            text("""INSERT INTO eval_runs (id, build_ref, suite, cases, passed, metrics, report)
                    VALUES (:id, :ref, :suite, :cases, :passed, CAST(:m AS jsonb), CAST(:r AS jsonb))"""),
            {
                "id": new_id(),
                "ref": ref,
                "suite": suite.suite,
                "cases": suite.cases,
                "passed": suite.passed,
                "m": json.dumps({**suite.metrics, "threshold": suite.threshold, "score": round(suite.score, 3)}),
                "r": json.dumps([asdict(r) for r in suite.results], default=str, ensure_ascii=False),
            },
        )


def write_report(ref: str, suites: list[SuiteResult]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{ref}.md"
    lines = [
        f"# Eval report, build `{ref}`",
        "",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M UTC} · business `{EVAL_SLUG}` · real Gemini",
        "",
        "| Suite | Score | Threshold | Result | Notes |",
        "|---|---|---|---|---|",
    ]
    for s in suites:
        m = s.metrics
        notes = ", ".join(
            f"{k} {v}" for k, v in m.items() if k in ("p50_s", "p95_s", "one_call_turns", "gate_repairs", "errors")
        )
        lines.append(
            f"| {s.suite} | {s.passed}/{s.cases} ({s.score:.0%}) | {s.threshold:.0%} | "
            f"{'✅' if s.ok else '❌'} | {notes} |"
        )
    for s in suites:
        failed = [r for r in s.results if not r.passed]
        if failed:
            lines += ["", f"## {s.suite}: failed cases", ""]
            lines += [f"- `{r.id}`: {r.detail}" for r in failed]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def recent(limit: int = 60) -> list[dict[str, Any]]:
    async with get_db().service_session(ServiceReason.ADMIN) as s:
        rows = (
            await s.execute(
                text("""SELECT id, build_ref, suite, cases, passed, metrics, report, created_at FROM eval_runs
                        ORDER BY created_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    return [
        {
            "id": str(r.id),
            "build_ref": r.build_ref,
            "suite": r.suite,
            "cases": r.cases,
            "passed": r.passed,
            "metrics": r.metrics,
            "failed": [c for c in (r.report or []) if not c.get("passed")],
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# the admin page's "Run" button: one run at a time, in the API process
_task: asyncio.Task[Any] | None = None
_log: list[str] = []


def start(suites: list[str] | None) -> bool:
    global _task
    if _task is not None and not _task.done():
        return False
    _log.clear()
    _task = asyncio.create_task(_run_logged(suites), name="evals")
    return True


async def _run_logged(suites: list[str] | None) -> None:
    try:
        ref, results = await run(suites, progress=_log.append)
        _log.append(f"done: build {ref}, {sum(s.ok for s in results)}/{len(results)} suites pass")
    except Exception as exc:
        log.exception("eval_run_failed")
        _log.append(f"failed: {type(exc).__name__}: {exc}")


def status() -> dict[str, Any]:
    return {"running": _task is not None and not _task.done(), "log": list(_log), "suites": ORDER}


def summary(results: list[SuiteResult]) -> str:
    return "\n".join(
        f"{s.suite:18s} {s.passed:3d}/{s.cases:<3d} {s.score:5.0%}  (≥{s.threshold:.0%})  {'PASS' if s.ok else 'FAIL'}"
        + (f"  p50 {s.metrics.get('p50_s')}s p95 {s.metrics.get('p95_s')}s" if s.suite in PIPELINE_SUITES else "")
        for s in results
    )


__all__ = ["ORDER", "SUITES", "THRESHOLDS", "recent", "run", "start", "status", "summary"]
