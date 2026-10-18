"""The dependency audit gate (BETA_10 §13; CHK-SEC-12)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_audit  # noqa: E402


@pytest.mark.check("CHK-SEC-12")
def test_high_and_critical_findings_fail_and_unknown_ones_are_reported() -> None:
    levels = {"V-1": "HIGH", "V-2": "CRITICAL", "V-3": "MODERATE", "V-4": None}
    findings = [("a", "1.0", "V-1"), ("b", "2.0", "V-2"), ("c", "3.0", "V-3"), ("d", "4.0", "V-4")]
    failing, unknown = check_audit.classify(findings, levels.get)
    assert failing == ["a 1.0: V-1 (HIGH)", "b 2.0: V-2 (CRITICAL)"]
    assert unknown == ["d 4.0: V-4 (severity unknown)"]


@pytest.mark.check("CHK-SEC-12")
def test_ci_runs_both_audits_on_every_change() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = " ".join(str(step.get("run", "")) for step in ci["jobs"]["lint"]["steps"])
    assert "check_audit.py pip" in steps and "check_audit.py npm" in steps
    assert "--audit-level=high" in (ROOT / "scripts" / "check_audit.py").read_text(encoding="utf-8")
