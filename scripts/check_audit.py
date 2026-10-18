"""Dependency audit (BETA_10 §13; CHK-SEC-12): fail on a high or critical vulnerability.

    python scripts/check_audit.py pip    # the locked Python dependencies, through pip-audit and OSV
    python scripts/check_audit.py npm    # the web app's production dependencies, through npm audit

pip-audit reports vulnerabilities without severity, so each finding is looked up on OSV
(https://api.osv.dev/v1/vulns/<id>): HIGH or CRITICAL fails the run; a finding whose severity is unknown is printed
and does not fail it. npm audit filters by severity itself (`--audit-level=high`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PIP_AUDIT = "pip-audit==2.9.0"
FAILING = {"HIGH", "CRITICAL"}


def severity(vuln_id: str) -> str | None:
    """OSV's severity label for a finding (GitHub advisories carry one), or None."""
    with urllib.request.urlopen(f"https://api.osv.dev/v1/vulns/{vuln_id}", timeout=20) as r:
        data: dict[str, Any] = json.load(r)
    label = (data.get("database_specific") or {}).get("severity")
    if label:
        return str(label).upper()
    for alias in data.get("aliases") or []:
        if alias.startswith("GHSA-") and alias != vuln_id:
            return severity(alias)
    return None


def classify(findings: list[tuple[str, str, str]], lookup: Any = severity) -> tuple[list[str], list[str]]:
    """(failing, unknown) lines for (package, version, vulnerability id) findings."""
    failing, unknown = [], []
    for name, version, vuln_id in findings:
        level = lookup(vuln_id)
        line = f"{name} {version}: {vuln_id} ({level or 'severity unknown'})"
        if level in FAILING:
            failing.append(line)
        elif level is None:
            unknown.append(line)
    return failing, unknown


def pip_findings() -> list[tuple[str, str, str]]:
    locked = subprocess.run(
        ["uv", "export", "--no-dev", "--no-hashes", "--format", "requirements-txt"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    requirements = "\n".join(ln for ln in locked.splitlines() if not ln.startswith(("-e", ".")))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(requirements)
    out = subprocess.run(
        ["uvx", PIP_AUDIT, "-r", f.name, "-f", "json", "--progress-spinner", "off", "--no-deps", "--disable-pip"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    data = json.loads(out)
    return [(d["name"], d["version"], v["id"]) for d in data["dependencies"] for v in d.get("vulns", [])]


def main(which: str) -> int:
    if which == "npm":
        return subprocess.run(
            [shutil.which("npm") or "npm", "audit", "--audit-level=high", "--omit=dev"],
            cwd=ROOT / "apps" / "admin",
            check=False,
        ).returncode
    failing, unknown = classify(pip_findings())
    for line in unknown:
        print(f"note: {line}")
    for line in failing:
        print(f"FAIL: {line}")
    print("audit: OK" if not failing else f"audit: {len(failing)} high or critical finding(s)")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pip"))
