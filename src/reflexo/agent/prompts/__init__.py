"""Versioned prompt files (POC_04 §11). `prompt_version` = name + short hash of the file, stored on agent_runs."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from string import Template

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=16)
def _load(name: str) -> tuple[Template, str]:
    raw = (_DIR / f"{name}.md").read_text(encoding="utf-8")
    return Template(raw), f"{name}@{hashlib.sha256(raw.encode()).hexdigest()[:10]}"


def prompt_version(name: str) -> str:
    return _load(name)[1]


def render(name: str, **values: str) -> str:
    """Fill `$placeholders`; a missing value is an error (no silent '$x' in production prompts)."""
    return _load(name)[0].substitute(**values)
