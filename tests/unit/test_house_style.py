"""House style: no long em-dash anywhere a person reads (web app, prompts, seeds, messages we send, voice)."""

from __future__ import annotations

import ast
from pathlib import Path

from reflexo.gateway.outbound import plain_blocks, plain_punctuation

ROOT = Path(__file__).resolve().parents[2]
DASH = "—"
# reads price ranges in imported menus ("1 kg — ₹950"): input, never shown
ALLOWED_LITERALS = {r"\d\s*[-–—]\s*\d"}


def test_outgoing_text_has_no_em_dash() -> None:
    assert plain_punctuation(f"Yes, we deliver to Kothrud {DASH} ₹50.") == "Yes, we deliver to Kothrud, ₹50."
    assert plain_punctuation(f"{DASH} first\nends with {DASH}") == "first\nends with"
    assert plain_punctuation("Tomorrow 4–8 pm") == "Tomorrow 4–8 pm"  # en-dash ranges stay
    blocks = plain_blocks(
        [{"type": "buttons", "text": f"Confirm {DASH} or change?", "options": [{"id": "y", "title": f"Yes {DASH} go"}]}]
    )
    assert DASH not in str(blocks)


def test_no_em_dash_in_what_people_read() -> None:
    web = [*(ROOT / "apps/admin/src").rglob("*.ts*"), ROOT / "apps/admin/index.html"]
    texts = [*web, *(ROOT / "src/reflexo").rglob("*.md"), *(ROOT / "config/seeds").glob("*.yaml")]
    offenders = [str(p.relative_to(ROOT)) for p in texts if DASH in p.read_text(encoding="utf-8")]
    assert offenders == [], f"em-dash in {offenders}"


def test_no_em_dash_in_python_string_literals() -> None:
    """Strings the platform says or shows; docstrings and comments are for developers."""
    offenders = []
    for p in (ROOT / "src/reflexo").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        if DASH not in src:
            continue
        tree = ast.parse(src)
        docs = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        offenders += [
            f"{p.relative_to(ROOT)}:{n.lineno}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and DASH in n.value
            and id(n) not in docs
            and n.value not in ALLOWED_LITERALS
        ]
    assert offenders == [], f"em-dash in string literals: {offenders}"
