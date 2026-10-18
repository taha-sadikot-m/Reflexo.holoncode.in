"""Check traceability (BETA_08 §2.4, CHK-Q-1).

    python scripts/check_traceability.py generate --from <path to beta/BETA_08_QUALITY.md>
    python scripts/check_traceability.py            # check (CI)

`generate` reads the check catalogue (BETA_08 §3) into `docs/checks.yaml`; the planning documents are not in this
repository, so the generated file is committed. `check` fails if:
- a test marker names a check that is not in the catalogue;
- an automated check of a delivered work package has no test marked with its id;
- a live, review or Lab check of a delivered work package has no entry in `docs/live_checks.md`;
- a bench, eval or load check of a delivered work package has neither a marked test nor an entry there.
Delivered work packages, and the "every WP that ..." checks they trigger, are listed in `docs/delivered.yaml`.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "docs" / "checks.yaml"
DELIVERED = ROOT / "docs" / "delivered.yaml"
LIVE = ROOT / "docs" / "live_checks.md"
MARKER = re.compile(r"@pytest\.mark\.check\(([^)]*)\)")
CHECK_ID = re.compile(r"CHK-[A-Z0-9]+(?:-R2)?-\d+")
WP = re.compile(r"WP-\d+[ab]?")
AUTOMATED = re.compile(r"\bA-(?:ui|u|i|w|c|p|e)\b")


def _expand(cell: str) -> list[str]:
    """`CHK-X-1`, `CHK-X-1 (R2)` or `CHK-X-1 to CHK-X-7`."""
    ids = CHECK_ID.findall(cell)
    if " to " in cell and len(ids) == 2:
        prefix, first = ids[0].rsplit("-", 1)
        last = int(ids[1].rsplit("-", 1)[1])
        return [f"{prefix}-{n}" for n in range(int(first), last + 1)]
    return ids[:1]


def _kinds(cell: str) -> list[str]:
    kinds = ["automated"] if AUTOMATED.search(cell) or cell.strip().startswith("§") else []
    for word, kind in (("Live", "live"), ("Review", "review"), ("Lab", "lab"), ("Eval", "eval"), ("Bench", "bench"),
                       ("Load", "load")):  # fmt: skip
        if word in cell:
            kinds.append(kind)
    return kinds


def _wps(check: str, cell: str) -> list[str]:
    """`WP-01`, `WP-02, rerun WP-25`, or `WP-01 (Q-1, Q-2), WP-25 (Q-3)` (which WP delivers which check)."""
    per_check = re.findall(r"(WP-\d+[ab]?)\s*\(([^)]*)\)", cell)
    if per_check and any(re.search(r"[A-Z]+-\d", inner) for _, inner in per_check):
        short = check.removeprefix("CHK-")
        return [wp for wp, inner in per_check if short in [x.strip() for x in inner.split(",")]]
    return WP.findall(cell)


def generate(beta_08: Path) -> dict[str, Any]:
    text = beta_08.read_text(encoding="utf-8")
    section = text[text.index("## 3. Check catalogue") : text.index("## 4. ")]
    checks: dict[str, Any] = {}
    release_2 = False
    for line in section.splitlines():
        if line.startswith("### "):
            release_2 = "Release 2" in line
        if not line.startswith("| ") or "---" in line or line.startswith("| Check"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for i in range(0, len(cells) - 2, 3):
            ids_cell, verified, wp = cells[i : i + 3]
            for cid in _expand(ids_cell):
                checks[cid] = {
                    "verified_by": verified,
                    "kinds": _kinds(verified),
                    "wps": _wps(cid, wp),
                    "wp_text": wp,
                    "release": 2 if release_2 or "(R2)" in ids_cell or "R2" in cid else 1,
                }
    return {"source": "beta/BETA_08_QUALITY.md §3", "checks": dict(sorted(checks.items()))}


def markers(tests: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for f in tests.rglob("test_*.py"):
        for m in MARKER.finditer(f.read_text(encoding="utf-8")):
            for cid in CHECK_ID.findall(m.group(1)):
                found.setdefault(cid, []).append(str(f.relative_to(ROOT)))
    return found


def check(catalogue: dict[str, Any], delivered: dict[str, Any], found: dict[str, list[str]], live: str) -> list[str]:
    checks = catalogue["checks"]
    wps = set(delivered.get("work_packages") or [])
    due = {cid for cid, c in checks.items() if wps & set(c["wps"])} | set(delivered.get("checks") or [])
    live_ids = set(CHECK_ID.findall(live))
    problems = [f"{cid}: marked in {', '.join(files)} but not in the catalogue" for cid, files in found.items()
                if cid not in checks]  # fmt: skip
    for cid in sorted(due):
        kinds = set(checks[cid]["kinds"])
        if "automated" in kinds and cid not in found:
            problems.append(f"{cid}: automated check of a delivered work package has no marked test")
        if kinds & {"live", "review", "lab"} and cid not in live_ids:
            problems.append(f"{cid}: live/review/lab check has no entry in docs/live_checks.md")
        if kinds & {"bench", "eval", "load"} and not kinds & {"automated"} and cid not in set(found) | live_ids:
            problems.append(f"{cid}: bench/eval/load check has no marked test and no entry in docs/live_checks.md")
    return problems


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", nargs="?", default="check", choices=["check", "generate"])
    p.add_argument("--from", dest="source", type=Path)
    a = p.parse_args(argv)
    if a.command == "generate":
        if a.source is None:
            p.error("generate needs --from <path to BETA_08_QUALITY.md>")
        data = generate(a.source)
        CHECKS.write_text(
            "# Generated by scripts/check_traceability.py from beta/BETA_08_QUALITY.md §3. Do not edit by hand.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
            encoding="utf-8",
        )
        print(f"{len(data['checks'])} checks written to {CHECKS.relative_to(ROOT)}")
        return 0
    problems = check(
        yaml.safe_load(CHECKS.read_text(encoding="utf-8")),
        yaml.safe_load(DELIVERED.read_text(encoding="utf-8")),
        markers(ROOT / "tests"),
        LIVE.read_text(encoding="utf-8") if LIVE.exists() else "",
    )
    for problem in problems:
        print(problem)
    print("traceability: OK" if not problems else f"traceability: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
