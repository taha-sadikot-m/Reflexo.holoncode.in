"""No real personal data in fixtures or eval datasets (BETA_08 §2.5, CHK-Q-6).

    python scripts/check_fixtures.py

Scans `tests/fixtures/`, `evals/datasets/` and `evals/benchmark/` for phone numbers, email addresses, street
addresses and common given names. Synthetic values that may appear are listed in `docs/fixture_safe.yaml`.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("tests/fixtures", "evals/datasets", "evals/benchmark")
SAFE = ROOT / "docs" / "fixture_safe.yaml"
SKIPPED = {"baseline.json"}  # generated benchmark measurements: numbers only, no conversation text

PHONE = re.compile(r"(?<![\w/])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# a house or flat number followed by a street-level word: an address someone lives at, not an area or a pincode
STREET = re.compile(
    r"\b(?:flat|house|plot|no\.?)?\s*\d{1,4}[a-z]?,?\s+[A-Z][a-z]+\s+(?:road|street|lane|marg|society|apartments?|colony)\b",
    re.IGNORECASE,
)
# common Indian given names (a privacy scan of fixture files, not customer text; extend when a fixture needs it)
GIVEN_NAMES = frozenset(
    """Aarav Aditi Aditya Akash Amit Ananya Anil Anita Anjali Ankit Anushka Arjun Aryan Deepak Divya Gaurav Ishaan
    Kavya Kiran Krishna Manish Meena Mohit Neha Nikhil Pooja Rajesh Ramesh Ravi Riya Rohan Rohit Sachin Sakshi Sanjay
    Shreya Simran Sneha Sunil Suresh Tanvi Varun Vikram Vivek Yash""".split()
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    value: str


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def scan_text(path: str, text: str, safe: dict[str, list[str]]) -> list[Finding]:
    prefixes = [_digits(p) for p in safe.get("phone_prefixes", [])]
    phones = {_digits(p)[-10:] for p in safe.get("phones", [])}
    domains = tuple(d.lower() for d in safe.get("email_domains", []))
    names = set(safe.get("names", []))
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in PHONE.finditer(line):
            d = _digits(m.group(0))
            if d[-10:] not in phones and not any(d.startswith(p) or ("91" + d).startswith(p) for p in prefixes):
                out.append(Finding(path, n, "phone", m.group(0)))
        for m in EMAIL.finditer(line):
            domain = m.group(1).lower()
            if not any(domain == d or domain.endswith("." + d) for d in domains):
                out.append(Finding(path, n, "email", m.group(0)))
        for m in STREET.finditer(line):
            out.append(Finding(path, n, "address", m.group(0).strip()))
        for word in re.findall(r"\b[A-Z][a-z]+\b", line):
            if word in GIVEN_NAMES and word not in names:
                out.append(Finding(path, n, "name", word))
    return out


def scan(root: Path = ROOT) -> list[Finding]:
    safe = yaml.safe_load(SAFE.read_text(encoding="utf-8"))
    findings = []
    for folder in SCANNED:
        base = root / folder
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in (".yaml", ".yml", ".json", ".csv", ".txt", ".md") and f.name not in SKIPPED:
                findings += scan_text(str(f.relative_to(root)), f.read_text(encoding="utf-8"), safe)
    return findings


def main() -> int:
    findings = scan()
    for f in findings:
        print(f"{f.path}:{f.line}: {f.kind} {f.value!r}")
    print("fixtures: OK" if not findings else f"fixtures: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
