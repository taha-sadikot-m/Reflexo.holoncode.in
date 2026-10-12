"""Eval suites (POC_08 §4). Usage: `python scripts/eval.py [suite ...] [--no-reseed]` (via `make eval [SUITE=…]`).

Needs the stack running (Postgres, Redis, Temporal, the pipeline worker) and REFLEXO_GOOGLE_API_KEY: the
pipeline suites go through the deployed worker with real Gemini. Writes `eval_runs` and `evals/reports/<ref>.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from reflexo.admin import evals
from reflexo.bootstrap import bootstrap


async def _main(suites: list[str], reseed: bool) -> int:
    bootstrap("reflexo-eval")
    ref, results = await evals.run([s for s in suites if s != "all"] or None, reseed=reseed, progress=print)
    print(f"\nbuild {ref}\n{evals.summary(results)}\nreport: evals/reports/{ref}.md")
    return 0 if all(s.ok for s in results) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Hindi and ₹ on a Windows console
    p = argparse.ArgumentParser()
    p.add_argument("suites", nargs="*", help=f"any of {', '.join(evals.ORDER)}; default (or 'all'): every suite")
    p.add_argument("--no-reseed", action="store_true", help="keep eval-bakes as it is")
    a = p.parse_args()
    sys.exit(asyncio.run(_main(a.suites, not a.no_reseed)))
