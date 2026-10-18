"""Where personal data lives (BETA_10 §2; CHK-SEC-1).

Every column that holds personal data is tagged in `config/personal_data.yaml`, in a category with its purpose, our
role under the DPDP Act and its retention. `docs/data_inventory.md` is generated from it (`scripts/data_inventory.py`),
and the schema test fails on a column whose name looks personal that is neither tagged nor reviewed as not personal.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Category:
    name: str
    purpose: str
    role: str  # Fiduciary | Processor | Both
    retention: str
    columns: dict[str, tuple[str, ...]]


REGISTRY = Path(__file__).resolve().parents[3] / "config" / "personal_data.yaml"


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[Category, ...], dict[tuple[str, str], str]]:
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    categories = tuple(
        Category(c["name"], c["purpose"], c["role"], c["retention"], {t: tuple(cs) for t, cs in c["columns"].items()})
        for c in data.get("categories", [])
    )
    reviewed = {tuple(r["column"].split(".", 1)): r["why"] for r in data.get("not_personal", [])}
    return categories, reviewed


def inventory() -> tuple[Category, ...]:
    return _load()[0]


def not_personal() -> dict[tuple[str, str], str]:
    return _load()[1]


# A column is checked when its name contains one of these (a name convention, never content).
PERSONAL_LOOKING = (
    "name", "email", "phone", "e164", "address", "content", "payload", "transcript", "summary", "memory", "notes",
    "question", "answer", "description", "attachment", "external_id", "caller", "details", "subject", "preview",
    "evidence", "ciphertext", "filename", "headers", "guess", "reason", "profile", "before", "after", "says",
)  # fmt: skip


def tagged() -> set[tuple[str, str]]:
    return {(table, col) for c in inventory() for table, cols in c.columns.items() for col in cols}


def looks_personal(column: str) -> bool:
    return any(word in column for word in PERSONAL_LOOKING)
