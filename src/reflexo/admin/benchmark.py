"""The benchmark set and its regression gate (BETA_12 §9; BETA_13 §7.5; ADR-025).

* Conversations are scenario files in `evals/benchmark/` (`groups.yaml` lists every group and when it becomes
  available). They run through the real ingress and the running worker, with real models, on a freshly reseeded
  eval-bakes, using the Simulation Lab's scenario engine. Shorthands: `say` (one message), `burst` (messages sent
  back to back), `owner_says` (the owner's phone), each with an `expect` condition.
* **Every measure is read from stored data** after the run (CHK-ARCH-6): reply times from `messages` and
  `outbound_messages`, cost from `model_calls`, calls from `agent_runs`, database work and coalescing from the
  X-ray (`pipeline_events`). The runner keeps no timers of its own.
* A run can be capped in rupees: no new conversation starts once the run's metered cost reaches the cap.
* The gate (§9.3): the median of the runs against the accepted baseline, per measure of §1.1. A measure fails when
  it is worse by more than `REFLEXO_BENCHMARK_REGRESSION_PCT` plus the baseline's noise band (the largest relative
  difference between any two of the baseline's runs); a §1.1 target missed is reported, and fails a release run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import text

from reflexo.admin import evals, scenarios
from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger
from reflexo.common.timeutil import utcnow

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[3] / "evals" / "benchmark"
BASELINE_FILE = ROOT / "baseline.json"  # the accepted baseline, versioned in git so CI and reviewers see it
CUSTOMER_CONCURRENCY = 4
EXPECT_TIMEOUT_S = 90

# BETA_12 §1.1 targets, in the units measured here. The gate compares these measures with the baseline.
TARGETS: dict[str, dict[str, float]] = {
    "chat_reply_ms": {"median": 3000, "p95": 6000},
    "cost_per_chat_turn_micro_inr": {"median": 150_000},
}
GATED: dict[str, tuple[str, ...]] = {"chat_reply_ms": ("median", "p95"), "cost_per_chat_turn_micro_inr": ("median",)}
HIGHER_IS_BETTER = frozenset({"one_call_turns", "cached_share"})
NOT_YET = {  # §7.1 measures whose feature does not exist yet, and the work package that brings it
    "typing_ms": "WP-03 (typing on real channels; the simulator has no typing call)",
    "voice_start_speaking_ms": "WP-16",
    "voice_call_ready_ms": "WP-16",
    "comment_to_dm_ms": "WP-03",
    "cost_per_voice_minute_micro_inr": "WP-16",
}


# ---------------------------------------------------------------- results


class Measure(BaseModel):
    median: float | None = None
    p95: float | None = None
    mean: float | None = None
    n: int = 0


class GroupOutcome(BaseModel):
    conversations: int = 0
    passed: int = 0
    failed: list[str] = Field(default_factory=list)
    skipped_budget: int = 0


class BenchmarkResult(BaseModel):
    """`benchmark_runs.results` (BETA_02 §5)."""

    set_version: str
    measures: dict[str, Measure] = Field(default_factory=dict)
    feature_cost: dict[str, dict[str, float]] = Field(default_factory=dict)  # feature -> {micro_inr_per_turn}
    not_available: dict[str, str] = Field(default_factory=dict)
    groups: dict[str, GroupOutcome] = Field(default_factory=dict)
    excluded_turns: dict[str, int] = Field(default_factory=dict)  # turns not in the reply-time gate, by outcome
    spent_micro_inr: int = 0
    unmetered_turns: int = 0  # turns with more model calls than metered rows: must stay 0 (CHK-PC-1)
    regressions: list[dict[str, Any]] = Field(default_factory=list)
    targets_missed: list[dict[str, Any]] = Field(default_factory=list)
    noise_pct: dict[str, float] = Field(default_factory=dict)  # baselines only: measure.stat -> band
    runs: int = 1  # baselines: how many runs were combined
    provisional: bool = False  # a baseline of fewer than 5 runs (BETA_12 §9.3)
    accepted_reason: str | None = None  # baselines accepted in the console: why (also written to _PROGRESS.md)
    accepted_by: str | None = None


# ---------------------------------------------------------------- the set


@dataclass
class Conversation:
    id: str
    group: str
    steps: list[dict[str, Any]]
    owner: bool = False

    def scenario(self) -> dict[str, Any]:
        actors = {} if self.owner else {"c": {"role": "customer"}}
        return {"id": self.id, "title": self.id, "actors": actors, "steps": self.steps}


def _expand(raw: dict[str, Any], group: str) -> Conversation:
    """A conversation in shorthand or full steps (BETA_13 §7.5): the expectation always ends it."""
    expect = {"wait_for": raw.get("expect"), "timeout_s": EXPECT_TIMEOUT_S}
    if "say" in raw:
        steps = [{"actor": "c", "send_text": raw["say"]}, expect]
    elif "burst" in raw:
        steps = [{"actor": "c", "send_text": m} for m in raw["burst"]] + [expect]
    elif "owner_says" in raw:
        steps = [{"actor": "owner", "send_text": raw["owner_says"]}, {**expect, "actor": "owner"}]
        return Conversation(raw["id"], group, steps, owner=True)
    else:
        steps = list(raw["steps"])
    return Conversation(raw["id"], group, steps)


def load_groups() -> list[dict[str, Any]]:
    return list(yaml.safe_load((ROOT / "groups.yaml").read_text(encoding="utf-8"))["groups"])


def load(groups: list[str] | None = None) -> list[Conversation]:
    out: list[Conversation] = []
    for g in load_groups():
        if not g.get("file") or (groups and g["id"] not in groups):
            continue
        data = yaml.safe_load((ROOT / g["file"]).read_text(encoding="utf-8"))
        out += [_expand(c, g["id"]) for c in data["conversations"]]
    return out


def set_version() -> str:
    """A hash of every benchmark file: two runs are comparable only on the same set."""
    h = hashlib.sha256()
    for f in sorted(ROOT.glob("*.yaml")):
        h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:12]


# ---------------------------------------------------------------- running


@dataclass
class RunState:
    business_id: UUID
    started_at: datetime
    budget_micro_inr: int | None
    groups: dict[str, GroupOutcome] = field(default_factory=dict)


async def spent_since(business_id: UUID, since: datetime) -> int:
    async with get_db().service_session(ServiceReason.COSTS) as s:
        return int(
            (
                await s.execute(
                    text("""SELECT COALESCE(sum(cost_micro_inr), 0) FROM model_calls
                            WHERE business_id = :b AND created_at >= :t"""),
                    {"b": business_id, "t": since},
                )
            ).scalar_one()
        )


async def _one(state: RunState, conv: Conversation, progress: Callable[[str], None]) -> None:
    outcome = state.groups.setdefault(conv.group, GroupOutcome())
    outcome.conversations += 1
    if state.budget_micro_inr is not None and await spent_since(state.business_id, state.started_at) >= (
        state.budget_micro_inr
    ):
        outcome.skipped_budget += 1
        return
    scenario = conv.scenario()
    run = scenarios.new_run(scenario, state.business_id)
    await scenarios.execute(run, scenario, state.business_id)
    if run.status == "passed":
        outcome.passed += 1
    else:
        outcome.failed.append(conv.id)
        failed = next((s for s in run.steps if s.status == "failed"), None)
        progress(f"  {conv.id}: {run.status} {failed.detail[:120] if failed and failed.detail else ''}")


async def run_once(
    groups: list[str] | None = None,
    *,
    budget_inr: float | None = None,
    reseed: bool = True,
    progress: Callable[[str], None] = print,
) -> tuple[UUID, BenchmarkResult]:
    """One benchmark run: the conversations, then the measures from stored data, stored in `benchmark_runs`."""
    shop = await evals.prepare(reseed)
    state = RunState(shop.business_id, utcnow(), int(budget_inr * 1_000_000) if budget_inr is not None else None)
    conversations = load(groups)
    customer = [c for c in conversations if not c.owner]
    owner = [c for c in conversations if c.owner]
    progress(f"benchmark: {len(customer)} customer and {len(owner)} owner conversations on eval-bakes")
    gate = asyncio.Semaphore(CUSTOMER_CONCURRENCY)

    async def bounded(c: Conversation) -> None:
        async with gate:
            await _one(state, c, progress)

    await asyncio.gather(*(bounded(c) for c in customer))
    for c in owner:  # one owner phone: its conversations run one after another
        await _one(state, c, progress)
    await asyncio.sleep(2)  # the last replies' delivery rows land
    result = await measure(shop.business_id, state.started_at, utcnow())
    result.groups = state.groups
    run_id = await _store(result, started_at=state.started_at)
    return run_id, result


# ---------------------------------------------------------------- measures (stored data only)


def _stats(values: list[float]) -> Measure:
    if not values:
        return Measure()
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))]
    return Measure(median=statistics.median(ordered), p95=p95, mean=statistics.fmean(ordered), n=len(ordered))


TURNS_SQL = """
SELECT t.id, t.desk, t.outcome, t.conversation_id, t.created_at,
       (SELECT max(m.created_at) FROM messages m WHERE m.id = ANY(t.message_ids)) AS last_in,
       (SELECT min(o.sent_at) FROM outbound_messages o JOIN messages m2 ON m2.id = o.message_id
         WHERE m2.turn_id = t.id) AS first_out,
       (SELECT r.model_calls FROM agent_runs r WHERE r.turn_id = t.id ORDER BY r.created_at DESC LIMIT 1) AS calls,
       (SELECT COALESCE(sum(c.cost_micro_inr), 0) FROM model_calls c WHERE c.turn_id = t.id) AS cost,
       (SELECT COALESCE(sum(c.input_tokens), 0) FROM model_calls c WHERE c.turn_id = t.id AND c.kind = 'chat') AS inp,
       (SELECT COALESCE(sum(c.cached_input_tokens), 0) FROM model_calls c
         WHERE c.turn_id = t.id AND c.kind = 'chat') AS cached,
       (SELECT COALESCE(sum(c.output_tokens), 0) FROM model_calls c WHERE c.turn_id = t.id AND c.kind = 'chat') AS out,
       (SELECT count(*) FROM model_calls c WHERE c.turn_id = t.id AND c.kind = 'chat') AS chat_calls,
       (SELECT (e.attrs->>'db_queries')::int FROM pipeline_events e
         WHERE e.turn_id = t.id AND e.stage = 'turn_finished' ORDER BY e.created_at DESC LIMIT 1) AS db_queries,
       (SELECT (e.attrs->>'coalesce_wait_ms')::int FROM pipeline_events e
         WHERE e.turn_id = t.id AND e.stage = 'turn_started' ORDER BY e.created_at LIMIT 1) AS coalesce_ms
FROM conversation_turns t
WHERE t.business_id = :b AND t.created_at >= :since AND t.created_at <= :until AND t.status = 'completed'
ORDER BY t.conversation_id, t.created_at
"""


async def measure(business_id: UUID, since: datetime, until: datetime) -> BenchmarkResult:
    """Every measure of BETA_01 §7.1 that exists today, and the lever measures of BETA_12 §2 and §3."""
    async with get_db().service_session(ServiceReason.COSTS) as s:
        turns = (await s.execute(text(TURNS_SQL), {"b": business_id, "since": since, "until": until})).all()
        features = (
            await s.execute(
                text("""SELECT feature, sum(cost_micro_inr) AS cost FROM model_calls
                        WHERE business_id = :b AND created_at >= :since AND created_at <= :until GROUP BY feature"""),
                {"b": business_id, "since": since, "until": until},
            )
        ).all()
    customer = [t for t in turns if str(t.desk) == "customer"]
    owner = [t for t in turns if str(t.desk) == "owner"]
    replied = [t for t in customer if str(t.outcome) == "replied" and t.last_in and t.first_out]
    excluded: dict[str, int] = {}
    for t in customer:
        if str(t.outcome) != "replied":
            excluded[str(t.outcome)] = excluded.get(str(t.outcome), 0) + 1

    def ms(t: Any) -> float:
        return float((t.first_out - t.last_in).total_seconds() * 1000)

    inp = sum(int(t.inp) for t in customer)
    m: dict[str, Measure] = {
        "chat_reply_ms": _stats([ms(t) for t in replied]),
        "owner_reply_ms": _stats([ms(t) for t in owner if t.last_in and t.first_out]),
        "cost_per_chat_turn_micro_inr": _stats([float(t.cost) for t in customer]),
        "model_calls_per_turn": _stats([float(t.calls or 0) for t in customer]),
        "input_tokens_per_call": _stats([int(t.inp) / int(t.chat_calls) for t in customer if t.chat_calls]),
        "output_tokens_per_turn": _stats([float(t.out) for t in customer]),
        "db_queries_per_turn": _stats([float(t.db_queries) for t in customer if t.db_queries is not None]),
        "coalesce_wait_ms": _stats([float(t.coalesce_ms) for t in customer if t.coalesce_ms is not None]),
    }
    one_call = [t for t in customer if t.calls is not None]
    m["one_call_turns"] = Measure(mean=_share(sum(1 for t in one_call if t.calls == 1), len(one_call)), n=len(one_call))
    m["cached_share"] = Measure(mean=_share(sum(int(t.cached) for t in customer), inp), n=len(customer))
    m["split_burst_rate"] = Measure(mean=_split_burst(customer), n=len(customer))
    turn_count = max(len(customer), 1)
    return BenchmarkResult(
        set_version=set_version(),
        measures=m,
        feature_cost={str(f.feature): {"micro_inr_per_turn": int(f.cost) / turn_count} for f in features},
        not_available=dict(NOT_YET),
        excluded_turns=excluded,
        spent_micro_inr=sum(int(f.cost) for f in features),
        unmetered_turns=sum(1 for t in turns if (t.calls or 0) > int(t.chat_calls)),
    )


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def _split_burst(turns: list[Any]) -> float | None:
    """Turns holding a customer message written before the previous turn's reply went out: the burst was split
    (BETA_12 §3, S5). Measured on what the customer wrote, so a scripted reply-then-answer is never counted."""
    if not turns:
        return None
    split = 0
    for previous, t in pairwise(turns):
        same = previous.conversation_id == t.conversation_id
        if same and previous.first_out is not None and t.last_in is not None and t.last_in < previous.first_out:
            split += 1
    return round(split / len(turns), 4)


# ---------------------------------------------------------------- baseline and gate


def _value(result: BenchmarkResult, name: str, stat: str) -> float | None:
    measure = result.measures.get(name)
    value = getattr(measure, stat) if measure else None
    return float(value) if value is not None else None


def combine(results: list[BenchmarkResult]) -> BenchmarkResult:
    """The median of several runs, per measure and statistic, with the noise band between the runs."""
    base = results[0].model_copy(deep=True)
    base.runs, base.provisional = len(results), len(results) < 5
    for name, measure in base.measures.items():
        for stat in ("median", "p95", "mean"):
            values = [v for r in results if (v := _value(r, name, stat)) is not None]
            setattr(measure, stat, statistics.median(values) if values else None)
            if len(values) >= 2 and min(values) > 0:
                base.noise_pct[f"{name}.{stat}"] = round((max(values) - min(values)) / min(values) * 100, 2)
    base.spent_micro_inr = sum(r.spent_micro_inr for r in results)
    return base


@dataclass
class Verdict:
    passed: bool
    regressions: list[dict[str, Any]]
    targets_missed: list[dict[str, Any]]
    provisional: bool


def gate(runs: list[BenchmarkResult], baseline: BenchmarkResult, *, release: bool = False) -> Verdict:
    """BETA_12 §9.3: the median across the runs against the accepted baseline (CHK-PC-10)."""
    current = combine(runs)
    allowed = get_settings().benchmark_regression_pct
    regressions, missed = [], []
    for name, stats in GATED.items():
        for stat in stats:
            now, then = _value(current, name, stat), _value(baseline, name, stat)
            if now is None or then is None or then == 0:
                continue
            delta = (now - then) / then * 100 * (-1 if name in HIGHER_IS_BETTER else 1)
            band = baseline.noise_pct.get(f"{name}.{stat}", 0.0)
            if delta > allowed + band:
                regressions.append(
                    {"measure": f"{name}.{stat}", "delta_pct": round(delta, 2), "allowed": allowed + band}
                )
            target = TARGETS.get(name, {}).get(stat)
            if target is not None and now > target:
                missed.append({"measure": f"{name}.{stat}", "value": now, "target": target})
    passed = not regressions and (not release or not missed)
    return Verdict(passed, regressions, missed, baseline.provisional)


# ---------------------------------------------------------------- storage


async def _store(
    result: BenchmarkResult, *, started_at: datetime, is_baseline: bool = False, compared_to: UUID | None = None
) -> UUID:
    run_id = new_id()
    async with get_db().service_session(ServiceReason.COSTS) as s:
        await s.execute(
            text("""INSERT INTO benchmark_runs (id, git_sha, set_version, started_at, finished_at, results,
                                                is_baseline, compared_to, passed)
                    VALUES (:id, :sha, :v, :start, now(), CAST(:r AS jsonb), :base, :cmp, :passed)"""),
            {
                "id": run_id,
                "sha": evals.build_ref(),
                "v": result.set_version,
                "start": started_at,
                "r": result.model_dump_json(),
                "base": is_baseline,
                "cmp": compared_to,
                "passed": None if is_baseline else not result.regressions,
            },
        )
    return run_id


async def load_runs(run_ids: list[UUID]) -> list[BenchmarkResult]:
    async with get_db().service_session(ServiceReason.COSTS) as s:
        rows = (
            await s.execute(text("SELECT results FROM benchmark_runs WHERE id = ANY(:ids)"), {"ids": run_ids})
        ).all()
    return [BenchmarkResult.model_validate(r.results) for r in rows]


async def current_baseline() -> tuple[UUID | None, BenchmarkResult] | None:
    """The accepted baseline for this set: from `benchmark_runs`, else from `evals/benchmark/baseline.json`."""
    async with get_db().service_session(ServiceReason.COSTS) as s:
        row = (
            await s.execute(
                text("""SELECT id, results FROM benchmark_runs WHERE is_baseline AND set_version = :v
                        ORDER BY started_at DESC LIMIT 1"""),
                {"v": set_version()},
            )
        ).first()
    if row is not None:
        return row.id, BenchmarkResult.model_validate(row.results)
    if BASELINE_FILE.exists():
        stored = BenchmarkResult.model_validate_json(BASELINE_FILE.read_text(encoding="utf-8"))
        if stored.set_version == set_version():
            return None, stored
    return None


async def accept_baseline(
    run_ids: list[UUID], *, reason: str | None = None, accepted_by: str | None = None, write_file: bool = True
) -> tuple[UUID, BenchmarkResult]:
    """Combine runs into the accepted baseline. The reason is written to `_PROGRESS.md` by the operator (§9.3).
    The console stores it with the baseline too, and never writes the file (`scripts/benchmark.py` does, for git)."""
    runs = await load_runs(run_ids)
    if len(runs) != len(set(run_ids)):
        raise ValueError("unknown benchmark run")
    if any(r.runs > 1 for r in runs):
        raise ValueError("a baseline cannot be combined again; pick the runs it was made from")
    combined = combine(runs).model_copy(update={"accepted_reason": reason, "accepted_by": accepted_by})
    if write_file:
        BASELINE_FILE.write_text(combined.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return await _store(combined, started_at=utcnow(), is_baseline=True), combined


async def history(limit: int = 50) -> dict[str, Any]:
    """The Benchmarks page (BETA_12 §8.1): the accepted baseline and the latest runs with their verdicts."""
    found = await current_baseline()
    async with get_db().service_session(ServiceReason.COSTS) as s:
        rows = (
            await s.execute(
                text("""SELECT id, git_sha, set_version, started_at, finished_at, results, is_baseline, compared_to,
                               passed FROM benchmark_runs ORDER BY started_at DESC LIMIT :n"""),
                {"n": limit},
            )
        ).all()
    baseline = None
    if found is not None:
        baseline_id, result = found
        baseline = {
            "id": str(baseline_id) if baseline_id else None,
            "source": "database" if baseline_id else "file",
            "results": result.model_dump(mode="json"),
        }
    return {
        "set_version": set_version(),
        "gated": {name: list(stats) for name, stats in GATED.items()},
        "targets": TARGETS,
        "higher_is_better": sorted(HIGHER_IS_BETTER),
        "allowed_pct": get_settings().benchmark_regression_pct,
        "baseline": baseline,
        "runs": [
            {
                "id": str(r.id),
                "git_sha": r.git_sha,
                "set_version": r.set_version,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "is_baseline": r.is_baseline,
                "compared_to": str(r.compared_to) if r.compared_to else None,
                "passed": r.passed,
                "results": r.results,
            }
            for r in rows
        ],
    }


async def compare(run_ids: list[UUID], *, release: bool = False) -> Verdict | None:
    """Gate a set of runs against the current baseline and record the verdict on each run."""
    found = await current_baseline()
    if found is None:
        return None
    baseline_id, baseline = found
    verdict = gate(await load_runs(run_ids), baseline, release=release)
    async with get_db().service_session(ServiceReason.COSTS) as s:
        await s.execute(
            text("""UPDATE benchmark_runs SET compared_to = :b, passed = :p,
                           results = jsonb_set(jsonb_set(results, '{regressions}', CAST(:r AS jsonb)),
                                               '{targets_missed}', CAST(:t AS jsonb))
                    WHERE id = ANY(:ids)"""),
            {
                "b": baseline_id,
                "p": verdict.passed,
                "r": json.dumps(verdict.regressions),
                "t": json.dumps(verdict.targets_missed),
                "ids": run_ids,
            },
        )
    return verdict
