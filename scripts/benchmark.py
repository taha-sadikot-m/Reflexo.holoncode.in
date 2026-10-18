"""The benchmark (BETA_12 §9). Needs the stack running (Postgres, Redis, Temporal, the pipeline worker) and a model key.

    python scripts/benchmark.py run [--runs N] [--budget-inr X] [--groups a,b] [--no-reseed] [--release]
    python scripts/benchmark.py accept RUN_ID [RUN_ID ...]     # combine runs into the accepted baseline
    python scripts/benchmark.py show RUN_ID

`run` stores each run in `benchmark_runs`, prints its measures, then gates the runs against the accepted baseline
(exit code 1 on a regression; with --release also on a missed target). `--budget-inr` caps the rupees spent across
all the runs: no conversation starts once the metered cost reaches it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from reflexo.admin import benchmark
from reflexo.bootstrap import bootstrap


def _print(run_id: UUID, r: benchmark.BenchmarkResult) -> None:
    print(f"\nrun {run_id}  set {r.set_version}  spent ₹{r.spent_micro_inr / 1e6:.2f}")
    for group, g in r.groups.items():
        print(f"  {group:<18} {g.passed}/{g.conversations} passed, {g.skipped_budget} skipped (budget)")
    for name, m in r.measures.items():
        parts = [f"{k} {v:,.1f}" for k, v in (("median", m.median), ("p95", m.p95), ("mean", m.mean)) if v is not None]
        print(f"  {name:<30} {', '.join(parts) or '-'}  (n={m.n})")
    print(f"  unmetered turns: {r.unmetered_turns}")
    if r.excluded_turns:
        print(f"  excluded from reply time: {r.excluded_turns}")
    if r.noise_pct:
        print(f"  noise band (%): {r.noise_pct}")


async def _run(a: argparse.Namespace) -> int:
    groups = a.groups.split(",") if a.groups else None
    ids: list[UUID] = []
    spent = 0
    for i in range(a.runs):
        left = None if a.budget_inr is None else a.budget_inr - spent / 1e6
        if left is not None and left <= 0:
            print(f"budget reached after {i} run(s)")
            break
        run_id, result = await benchmark.run_once(groups, budget_inr=left, reseed=not a.no_reseed)
        spent += result.spent_micro_inr
        ids.append(run_id)
        _print(run_id, result)
    verdict = await benchmark.compare(ids, release=a.release) if ids else None
    if verdict is None:
        print("\nno accepted baseline for this benchmark set yet: accept one with `benchmark.py accept <run ids>`")
        return 0
    print(f"\ngate: {'PASS' if verdict.passed else 'FAIL'}{' (provisional baseline)' if verdict.provisional else ''}")
    for reg in verdict.regressions:
        print(f"  regression {reg['measure']}: {reg['delta_pct']:+.1f}% (allowed {reg['allowed']:.1f}%)")
    for miss in verdict.targets_missed:
        print(f"  target missed {miss['measure']}: {miss['value']:,.0f} > {miss['target']:,.0f}")
    return 0 if verdict.passed else 1


async def _main(a: argparse.Namespace) -> int:
    bootstrap("reflexo-benchmark")
    if a.command == "run":
        return await _run(a)
    if a.command == "accept":
        run_id, combined = await benchmark.accept_baseline([UUID(x) for x in a.run_ids])
        _print(run_id, combined)
        print(f"\naccepted as the baseline ({combined.runs} run(s){', provisional' if combined.provisional else ''})")
        return 0
    [result] = await benchmark.load_runs([UUID(a.run_ids[0])])
    _print(UUID(a.run_ids[0]), result)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--budget-inr", type=float, default=None)
    r.add_argument("--groups", default="")
    r.add_argument("--no-reseed", action="store_true")
    r.add_argument("--release", action="store_true", help="a missed §1.1 target fails the gate too")
    for name in ("accept", "show"):
        sub.add_parser(name).add_argument("run_ids", nargs="+")
    sys.exit(
        asyncio.run(_main(p.parse_args()), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
    )
