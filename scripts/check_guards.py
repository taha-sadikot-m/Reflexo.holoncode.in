"""Forbidden-pattern guard (POC_08 §6 `guards`): no eval/exec, no LangGraph interrupt/stores,
and no old-architecture names in the codebase (POC_01 §12.1). Exit code 1 on hits."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = [ROOT / "src", ROOT / "migrations" / "versions"]
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("eval() is forbidden", re.compile(r"(?<![\w.])eval\s*\(")),
    ("exec() is forbidden", re.compile(r"(?<![\w.])exec\s*\(")),
    ("LangGraph interrupt() is forbidden (approvals use Temporal)", re.compile(r"\binterrupt\s*\(")),
    (
        "LangGraph stores are forbidden (memory lives in Postgres)",
        re.compile(r"\b(InMemoryStore|BaseStore|PostgresStore)\b"),
    ),
    (
        "old schema name",
        re.compile(r"\b(business_channels|connected_accounts|channel_messages|customer_channel_identities)\b"),
    ),
    ("old architecture: internal MCP client", re.compile(r"\bMultiServerMCPClient\b")),
    (
        "old architecture: Celery/Mem0/Qdrant/NeMo",
        re.compile(r"^\s*(import|from)\s+(celery|mem0|qdrant_client|nemoguardrails)\b", re.M),
    ),
]


def main() -> int:
    hits: list[str] = []
    for base in SCAN:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for label, rx in RULES:
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    hits.append(f"{path.relative_to(ROOT)}:{line}: {label}")
    for h in hits:
        print(h)
    print(f"guards: {'OK' if not hits else f'{len(hits)} hit(s)'}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
