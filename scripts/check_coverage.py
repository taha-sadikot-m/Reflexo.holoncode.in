"""Branch coverage gates (BETA_08 §2.4, CHK-Q-2).

    pytest --cov=reflexo --cov-branch --cov-report=json:coverage.json ...
    python scripts/check_coverage.py [coverage.json] [--update-baseline]

Fails if a safety-core module has less than 90% branch coverage, or total branch coverage fell by more than 0.5
points against `coverage_baseline.json`. Safety-core modules that do not exist yet are skipped until they do.
`--update-baseline` writes the current total (only when it did not fall).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "coverage_baseline.json"
FLOOR_PCT = 90.0
RATCHET_POINTS = 0.5
# BETA_08 §2.4: the safety core (a folder means every file in it)
SAFETY_CORE = (
    "src/reflexo/policy/",
    "src/reflexo/agent/output_gate.py",
    "src/reflexo/vault/",
    "src/reflexo/gateway/policy.py",
    "src/reflexo/automations/validator.py",
    "src/reflexo/automations/prove.py",
    "src/reflexo/commerce/payments.py",
    "src/reflexo/domains/translators/",
    "src/reflexo/domains/manifest.py",
    "src/reflexo/workflows/privacy.py",
    "src/reflexo/workflows/sentinel.py",
)


def _pct(summary: dict[str, Any]) -> float:
    branches = int(summary.get("num_branches", 0))
    return 100.0 if branches == 0 else 100.0 * int(summary.get("covered_branches", 0)) / branches


def evaluate(report: dict[str, Any], baseline_total: float | None) -> tuple[list[str], float]:
    files = {path.replace("\\", "/"): data["summary"] for path, data in report["files"].items()}
    problems = []
    for core in SAFETY_CORE:
        matched = {p: s for p, s in files.items() if p == core or (core.endswith("/") and p.startswith(core))}
        if not matched:
            continue  # not built yet
        num = sum(int(s.get("num_branches", 0)) for s in matched.values())
        covered = sum(int(s.get("covered_branches", 0)) for s in matched.values())
        pct = 100.0 if num == 0 else 100.0 * covered / num
        if pct < FLOOR_PCT:
            problems.append(f"{core}: branch coverage {pct:.1f}% < {FLOOR_PCT:.0f}%")
    total = _pct(report["totals"])
    if baseline_total is not None and total < baseline_total - RATCHET_POINTS:
        problems.append(
            f"total branch coverage {total:.1f}% fell more than {RATCHET_POINTS} below {baseline_total:.1f}%"
        )
    return problems, total


def main(argv: list[str]) -> int:
    path = Path(next((a for a in argv if not a.startswith("--")), "coverage.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["total_branch_pct"] if BASELINE.exists() else None
    problems, total = evaluate(report, baseline)
    for p in problems:
        print(p)
    if "--update-baseline" in argv and not problems:
        BASELINE.write_text(json.dumps({"total_branch_pct": round(total, 2)}, indent=2) + "\n", encoding="utf-8")
    print(f"coverage: total branch {total:.1f}%" + (" OK" if not problems else f", {len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
