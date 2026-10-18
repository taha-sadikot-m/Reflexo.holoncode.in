"""No new fixed sentences for people in code (BETA_13 §6.3; CHK-MSG-4).

    python scripts/check_fixed_text.py            # check (CI)
    python scripts/check_fixed_text.py --write    # rewrite docs/fixed_text_baseline.txt (only ever to shrink it)

Messages are written by purpose and checked (`reflexo.agent.messages`). The only fixed wording allowed lives in
`config/messages/fallbacks/`, the Meta templates, and the sentences the POC already had, listed in
`docs/fixed_text_baseline.txt`, each moving to a purpose when its module is next changed. The check fails if code
holds a sentence a person could read that is not in the baseline, if the baseline lists a sentence the code no
longer has (remove the line), or if the baseline grew compared with the last commit.

What counts as a sentence a person could read: a string literal (an f-string's fixed parts) with at least three
words that ends like a sentence, holds an emoji, or has at least five words. Text written for the model or for
operators is not person-facing and is skipped by where it stands: docstrings, log and X-ray calls, raised errors,
SQL, prompts, tool descriptions and guidance, and tool results for the model (`next`, `guidance`, ...).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "reflexo"
BASELINE = ROOT / "docs" / "fixed_text_baseline.txt"
SKIP_DIRS = {"prompts", "admin"}  # model prompts; the operators' console (not customer- or owner-facing)
MODEL_KEYWORDS = {"description", "guidance", "help", "title", "detail", "reason", "examples", "context"}
MODEL_KEYS = {"next", "guidance", "tell_customer", "instruction", "hint", "detail", "say_to_model", "description"}
LOG_NAMES = {"log", "logger", "logging", "warnings", "emit", "print", "span", "set_attribute", "add_event"}
SQL_CALLS = {"text", "sql"}
MODEL_CALLS = {"ChatMessage", "user", "system", "assistant", "render", "SystemMessage", "HumanMessage", "Field"}
_WORD = re.compile(r"[A-Za-zऀ-ॿ]{2,}")
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_SENTENCE_END = re.compile(r"[.!?।]\s*$")


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _root_name(node: ast.AST) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Call):
        return _root_name(node.func)
    return node.id if isinstance(node, ast.Name) else ""


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


_PATTERN = re.compile(r"\\[bdswBDSW]|\(\?|\{\{.*\}\}$|^\s*[\w.]+\s*(==|!=|<|>)|::")  # regexes, CEL, Cedar


def is_sentence(text: str) -> bool:
    if _PATTERN.search(text) or text.lstrip().startswith("#"):  # patterns; prompt section headers
        return False
    words = _WORD.findall(text)
    if len(words) < 3 or " " not in text.strip():
        return False
    if text.lstrip().startswith(("SELECT", "INSERT", "UPDATE", "DELETE", "WITH ", "CREATE", "ALTER")):
        return False
    return bool(_SENTENCE_END.search(text) or _EMOJI.search(text) or len(words) >= 5)


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[str] = []
        self._skip_depth = 0

    def _skipping(self, node: ast.AST) -> None:
        self._skip_depth += 1
        self.generic_visit(node)
        self._skip_depth -= 1

    def visit_Expr(self, node: ast.Expr) -> None:  # docstrings and bare strings
        if _literal(node.value) is not None:
            return
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self._skipping(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._skipping(node)

    def visit_Call(self, node: ast.Call) -> None:
        name, root = _callee(node), _root_name(node.func)
        if root in LOG_NAMES or name in LOG_NAMES or name in SQL_CALLS or name in MODEL_CALLS:
            self._skipping(node)
            return
        for kw in node.keywords:
            if kw.arg in MODEL_KEYWORDS:
                self._skip_depth += 1
                self.visit(kw.value)
                self._skip_depth -= 1
            else:
                self.visit(kw.value)
        self.visit(node.func)
        for arg in node.args:
            self.visit(arg)

    def _model_target(self, target: ast.AST) -> bool:
        if isinstance(target, ast.Subscript):
            return _literal(target.slice) in MODEL_KEYS
        return isinstance(target, ast.Name) and target.id in MODEL_KEYS

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(self._model_target(t) for t in node.targets):
            self._skipping(node)
            return
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self._model_target(node.target):
            self._skipping(node)
            return
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if key is not None and _literal(key) in MODEL_KEYS:
                self._skip_depth += 1
                self.visit(value)
                self._skip_depth -= 1
            else:
                if key is not None:
                    self.visit(key)
                self.visit(value)

    def _string(self, node: ast.AST) -> None:
        text = _literal(node)
        if text is not None and not self._skip_depth and is_sentence(text):
            self.found.append(" ".join(text.split()))

    def visit_Constant(self, node: ast.Constant) -> None:
        self._string(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._string(node)


def scan(src: Path = SRC) -> list[tuple[str, str]]:
    out = []
    for path in sorted(src.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(src).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        s = _Scan()
        s.visit(tree)
        rel = "src/reflexo/" + path.relative_to(src).as_posix()
        out += [(rel, text) for text in s.found]
    return out


def read_baseline(text: str | None = None) -> list[tuple[str, str]]:
    raw = BASELINE.read_text(encoding="utf-8") if text is None else text
    rows = []
    for line in raw.splitlines():
        if line.strip() and not line.startswith("#"):
            path, _, sentence = line.partition(" | ")
            rows.append((path, sentence))
    return rows


def committed_count() -> int | None:
    try:
        old = subprocess.run(
            ["git", "show", f"HEAD:{BASELINE.relative_to(ROOT).as_posix()}"],
            cwd=ROOT, capture_output=True, text=True, check=True, encoding="utf-8",
        ).stdout  # fmt: skip
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return len(read_baseline(old))


HEADER = """# Fixed sentences for people that the POC code already had (BETA_13 §6.3; CHK-MSG-4).
# Each moves to a message purpose (config/messages/purposes.yaml) when its module is next changed.
# This file may only shrink: scripts/check_fixed_text.py fails on any sentence not listed here.
# Format: <file> | <sentence, whitespace collapsed; {} = a value filled in>
"""


def problems() -> list[str]:
    found = Counter(text for _, text in scan())
    listed = Counter(text for _, text in read_baseline())
    out = [f"new fixed sentence (write it by purpose instead): {t!r}" for t in sorted(found - listed)]
    out += [f"no longer in code, remove it from the baseline: {t!r}" for t in sorted(listed - found)]
    before = committed_count()
    now = sum(listed.values())
    if before is not None and now > before:
        out.append(f"docs/fixed_text_baseline.txt grew from {before} to {now} lines; it may only shrink")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    if parser.parse_args().write:
        BASELINE.write_text(HEADER + "".join(f"{p} | {t}\n" for p, t in scan()), encoding="utf-8")
        return 0
    found = problems()
    for p in found:
        print(p)
    print("fixed text: OK" if not found else f"fixed text: {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
