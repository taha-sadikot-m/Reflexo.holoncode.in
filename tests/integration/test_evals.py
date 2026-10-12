"""Eval suites (POC_08 §4): datasets are well-formed, and the deterministic suite (the gate on forced replies) runs
in CI against a freshly seeded eval-bakes — 100 % of adversarial replies blocked, every control passes."""

from __future__ import annotations

import pytest

from reflexo.admin import evals
from reflexo.admin.seeding import reset
from reflexo.common.db import Database

pytestmark = pytest.mark.integration

REQUIRED = {
    "tool_selection": {"messages"},
    "gate_adversarial": {"text"},
    "grounding": {"messages"},
    "language": {"message", "expected_lang"},
    "ask_owner_scope": {"question", "answer", "expected_scope"},
    "owner_commands": {"messages"},
    "builder": {"description", "expect"},
    "learning": {"capability", "situation", "approve"},
}


@pytest.mark.parametrize("suite", evals.ORDER)
def test_dataset_is_well_formed(suite: str) -> None:
    cases = evals.load_cases(suite)
    assert cases, f"{suite} has no cases"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in cases:
        assert REQUIRED[suite] <= set(c), (c["id"], REQUIRED[suite] - set(c))


def test_script_check() -> None:
    assert evals.script_matches("रेड वेलवेट केक ₹1,100 का है", "hi")
    assert evals.script_matches("Red Velvet 1 kg ₹1,100 hai", "hi-Latn")
    assert not evals.script_matches("Red Velvet 1 kg ₹1,100 hai", "hi")


async def test_gate_adversarial_suite(db: Database) -> None:
    await reset(db, "full", slugs=(evals.EVAL_SLUG,))
    shop = await evals.prepare(reseed=False)
    results = await evals.SUITES["gate_adversarial"](shop, evals.load_cases("gate_adversarial"), "ci")
    failed = [f"{r.id}: {r.detail}" for r in results if not r.passed]
    assert not failed, failed
