"""Module boundary check (POC_01 §4.1).

Rules:
1. A module may import another reflexo module only if the dependency is allowed in ALLOWED.
2. Cross-module imports must target `reflexo.<module>.api` exactly (never internals).
3. `reflexo.common` imports nothing else from reflexo. `bootstrap`, `api` (HTTP), `admin`, `workflows` may import
   any module's api.
Exit code 1 on violations.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "reflexo"
ALL = {
    "common",
    "pipeline",
    "gateway",
    "agent",
    "voice",
    "knowledge",
    "commerce",
    "automations",
    "integrations",
    "approvals",
    "owner",
    "admin",
    "workflows",
    "api",
}
ALLOWED: dict[str, set[str]] = {
    "common": set(),
    "pipeline": {"common"},
    "gateway": {"pipeline", "common"},
    "agent": {"knowledge", "commerce", "integrations", "approvals", "owner", "gateway", "common"},
    "voice": {"agent", "gateway", "commerce", "knowledge", "common"},
    "knowledge": {"common"},
    "commerce": {"knowledge", "integrations", "common"},
    "automations": {"gateway", "commerce", "knowledge", "approvals", "agent", "pipeline", "common"},
    "integrations": {"common"},
    "approvals": {"gateway", "common"},
    "owner": {"gateway", "knowledge", "approvals", "common"},
    "admin": ALL - {"admin", "api"},
    "workflows": ALL - {"workflows", "api"},
    "api": ALL - {"api"},
}
OPEN_INTERNALS = {"common"}  # common's submodules may be imported directly by everyone


def module_of(path: Path) -> str | None:
    rel = path.relative_to(ROOT).parts
    if len(rel) == 1:  # reflexo/bootstrap.py, reflexo/__init__.py
        return None
    return rel[0]


def imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module))
    return out


def check() -> list[str]:
    errors: list[str] = []
    for path in ROOT.rglob("*.py"):
        src_mod = module_of(path)
        if src_mod is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, name in imported_names(tree):
            parts = name.split(".")
            if parts[0] != "reflexo" or len(parts) < 2 or parts[1] not in ALL:
                continue
            dst = parts[1]
            if dst == src_mod:
                continue
            where = f"{path.relative_to(ROOT.parent.parent)}:{lineno}"
            if dst not in ALLOWED[src_mod]:
                errors.append(f"{where}: `{src_mod}` may not import `{dst}` ({name})")
            elif dst not in OPEN_INTERNALS and parts[2:3] != ["api"]:
                errors.append(f"{where}: import `reflexo.{dst}.api`, not internals ({name})")
    return errors


if __name__ == "__main__":
    problems = check()
    for p in problems:
        print(p)
    print(f"module boundaries: {'OK' if not problems else f'{len(problems)} violation(s)'}")
    sys.exit(1 if problems else 0)
