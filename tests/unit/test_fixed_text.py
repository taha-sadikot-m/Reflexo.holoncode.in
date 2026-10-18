"""No new fixed sentences for people in code (BETA_13 §6.3; CHK-MSG-4)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_fixed_text as guard


@pytest.mark.check("CHK-MSG-4")
def test_code_holds_no_sentence_for_people_outside_the_baseline() -> None:
    assert guard.problems() == []


@pytest.mark.check("CHK-MSG-4")
def test_the_guard_sees_a_new_sentence_and_skips_text_for_the_model() -> None:
    source = """
def f(notifier, log):
    log.info("Customer {} paid a lot of money today.")  # a log line
    notify(text=f"Your order {n} is confirmed, thank you!")  # for a person: caught
    return {"next": "Tell the customer it is done, briefly and warmly.", "say": "Order placed for you today 🎂"}
"""
    scan = guard._Scan()
    scan.visit(ast.parse(source))
    assert scan.found == ["Your order {} is confirmed, thank you!", "Order placed for you today 🎂"]


def test_the_baseline_only_lists_sentences_the_code_still_has() -> None:
    listed = {text for _, text in guard.read_baseline()}
    assert listed == {text for _, text in guard.scan()}
