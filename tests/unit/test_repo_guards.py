"""The repository passes its own module-boundary and forbidden-pattern checks (POC_08 T29)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_module_boundaries_clean() -> None:
    assert _load("check_boundaries").check() == []


def test_forbidden_patterns_clean() -> None:
    assert _load("check_guards").main() == 0


def test_boundary_checker_catches_violations(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    checker = _load("check_boundaries")
    root = tmp_path / "src" / "reflexo"
    (root / "knowledge").mkdir(parents=True)
    (root / "knowledge" / "x.py").write_text(
        "from reflexo.agent.api import thing\nfrom reflexo.common.db import get_db\n"
    )
    (root / "agent").mkdir()
    (root / "agent" / "y.py").write_text("from reflexo.knowledge.search import s\n")
    monkeypatch.setattr(checker, "ROOT", root)
    errors = checker.check()
    assert any("`knowledge` may not import `agent`" in e for e in errors)
    assert any("import `reflexo.knowledge.api`, not internals" in e for e in errors)
    assert len(errors) == 2
