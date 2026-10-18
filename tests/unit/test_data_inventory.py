"""The data inventory is generated from the personal-data registry (BETA_10 §2; CHK-SEC-1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reflexo.common import personal_data

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import data_inventory

DOC = Path(__file__).resolve().parents[2] / "docs" / "data_inventory.md"


@pytest.mark.check("CHK-SEC-1")
def test_the_inventory_lists_every_tagged_column() -> None:
    assert DOC.read_text(encoding="utf-8") == data_inventory.render_markdown(), "run scripts/data_inventory.py"
    for table, column in personal_data.tagged():
        assert f"`{table}`" in DOC.read_text(encoding="utf-8") and column in DOC.read_text(encoding="utf-8")


def test_a_column_is_either_personal_or_reviewed_never_both() -> None:
    assert not personal_data.tagged() & set(personal_data.not_personal())
