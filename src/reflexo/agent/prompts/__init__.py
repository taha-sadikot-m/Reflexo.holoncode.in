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
    from reflexo.common.config import get_settings  # the language policy changes the prompt: part of its version

    version = hashlib.sha256(f"{raw}|{get_settings().language_policy}".encode()).hexdigest()[:10]
    return Template(raw), f"{name}@{version}"


def prompt_version(name: str) -> str:
    return _load(name)[1]


def render(name: str, **values: str) -> str:
    """Fill `$placeholders`; a missing value is an error (no silent '$x' in production prompts). `$language_rule`
    is the platform's language policy for the prompt's audience (English only, or mirror the person)."""
    from reflexo.agent.language import language_rule

    values.setdefault("language_rule", language_rule("owner" if name == "owner" else "customer"))
    return _load(name)[0].substitute(**values)
