"""The CI quality gates themselves (WP-01; BETA_08 §2.4, §2.5): traceability, coverage, fixture privacy."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.check("CHK-Q-1")
def test_every_delivered_check_is_traced() -> None:
    t = script("check_traceability")
    import yaml

    catalogue = yaml.safe_load((ROOT / "docs" / "checks.yaml").read_text(encoding="utf-8"))
    delivered = yaml.safe_load((ROOT / "docs" / "delivered.yaml").read_text(encoding="utf-8"))
    live = (
        (ROOT / "docs" / "live_checks.md").read_text(encoding="utf-8")
        if (ROOT / "docs" / "live_checks.md").exists()
        else ""
    )
    assert t.check(catalogue, delivered, t.markers(ROOT / "tests"), live) == []


def test_traceability_catches_unknown_and_untested_checks() -> None:
    t = script("check_traceability")
    catalogue = {
        "checks": {
            "CHK-X-1": {"kinds": ["automated"], "wps": ["WP-01"]},
            "CHK-X-2": {"kinds": ["live"], "wps": ["WP-01"]},
            "CHK-X-3": {"kinds": ["bench"], "wps": ["WP-01"]},
            "CHK-X-4": {"kinds": ["automated"], "wps": ["WP-09"]},  # not delivered: not required yet
        }
    }
    problems = t.check(catalogue, {"work_packages": ["WP-01"]}, {"CHK-Y-9": ["tests/x.py"]}, "")
    assert any("CHK-Y-9" in p and "not in the catalogue" in p for p in problems)
    assert any(p.startswith("CHK-X-1:") for p in problems)
    assert any(p.startswith("CHK-X-2:") for p in problems) and any(p.startswith("CHK-X-3:") for p in problems)
    assert not any(p.startswith("CHK-X-4:") for p in problems)
    assert t.check(catalogue, {"work_packages": ["WP-01"]}, {"CHK-X-1": ["t"], "CHK-X-3": ["t"]}, "## CHK-X-2") == []


def test_traceability_reads_ranges_and_per_check_work_packages() -> None:
    t = script("check_traceability")
    assert t._expand("CHK-Q-1 to CHK-Q-3") == ["CHK-Q-1", "CHK-Q-2", "CHK-Q-3"]
    assert t._expand("CHK-CH-20 (R2)") == ["CHK-CH-20"]
    cell = "WP-01 (Q-1, Q-2, Q-6), WP-25 (Q-3, Q-4)"
    assert t._wps("CHK-Q-2", cell) == ["WP-01"] and t._wps("CHK-Q-4", cell) == ["WP-25"]
    assert t._wps("CHK-TM-4", "WP-32 (WhatsApp, Telegram), WP-33 (Slack)") == ["WP-32", "WP-33"]


@pytest.mark.check("CHK-Q-2")
def test_coverage_floor_and_ratchet() -> None:
    c = script("check_coverage")

    def report(payments: tuple[int, int], total: tuple[int, int]) -> dict[str, object]:
        return {
            "files": {
                "src\\reflexo\\commerce\\payments.py": {
                    "summary": {"num_branches": payments[0], "covered_branches": payments[1]}
                },
                "src/reflexo/agent/output_gate.py": {"summary": {"num_branches": 10, "covered_branches": 10}},
                "src/reflexo/agent/desk.py": {"summary": {"num_branches": 100, "covered_branches": 10}},  # not core
            },
            "totals": {"num_branches": total[0], "covered_branches": total[1]},
        }

    assert c.evaluate(report((100, 95), (1000, 700)), 70.0) == ([], 70.0)
    low, _ = c.evaluate(report((100, 85), (1000, 700)), 70.0)
    assert len(low) == 1 and "commerce/payments.py" in low[0]
    fell, _ = c.evaluate(report((100, 95), (1000, 690)), 70.0)
    assert len(fell) == 1 and "fell" in fell[0]  # 69% is more than 0.5 points below 70%
    assert c.evaluate(report((100, 95), (1000, 698)), 70.0)[0] == []  # within the ratchet


@pytest.mark.check("CHK-Q-6")
def test_no_real_personal_data_in_fixtures_or_datasets() -> None:
    f = script("check_fixtures")
    assert f.scan() == []
    safe = {
        "phone_prefixes": ["+9199999"],
        "phones": ["9876543210"],
        "email_domains": ["example.com"],
        "names": ["Meera"],
    }
    found = f.scan_text(
        "x.yaml",
        "Call Anjali on +91 98220 12345 or anjali@gmail.com, flat 12 Lake Road. Meera at +91 99999 01234, "
        "a@example.com, dummy 919876543210, Kothrud 411038.",
        safe,
    )
    assert sorted({x.kind for x in found}) == ["address", "email", "name", "phone"]
    assert {x.value for x in found if x.kind == "phone"} == {"+91 98220 12345"}
    assert [x.value for x in found if x.kind == "name"] == ["Anjali"]
