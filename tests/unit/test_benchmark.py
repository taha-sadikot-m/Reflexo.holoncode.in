"""The benchmark set and its gate (WP-01; BETA_12 §9)."""

from __future__ import annotations

import pytest

from reflexo.admin import benchmark
from reflexo.admin.benchmark import BenchmarkResult, Measure


def result(reply_median: float, reply_p95: float = 6000, cost: float = 700_000) -> BenchmarkResult:
    return BenchmarkResult(
        set_version="test",
        measures={
            "chat_reply_ms": Measure(median=reply_median, p95=reply_p95, n=100),
            "cost_per_chat_turn_micro_inr": Measure(median=cost, n=100),
        },
    )


def baseline_of(*medians: float) -> BenchmarkResult:
    return benchmark.combine([result(m) for m in medians])


@pytest.mark.check("CHK-PC-10")
def test_gate_fails_15_percent_slower_and_passes_within_the_noise_band() -> None:
    baseline = baseline_of(5000, 5050, 5020, 5100, 5000)  # median 5,020 ms; band (5,100 - 5,000) / 5,000 = 2%
    assert baseline.runs == 5 and not baseline.provisional
    assert baseline.noise_pct["chat_reply_ms.median"] == pytest.approx(2.0)

    slower = benchmark.gate([result(5020 * 1.15)] * 3, baseline)
    assert not slower.passed and [r["measure"] for r in slower.regressions] == ["chat_reply_ms.median"]

    noisy = benchmark.gate([result(5020 * 1.12), result(5020 * 1.10), result(5020 * 1.11)], baseline)
    assert noisy.passed and noisy.regressions == []  # 11% worse is inside 10% + the 2% band


def test_gate_uses_the_median_of_the_runs_and_reports_targets_separately() -> None:
    baseline = baseline_of(2800, 2900, 2850, 2800, 2950)
    one_bad_run = benchmark.gate([result(2850), result(9000), result(2900)], baseline)
    assert one_bad_run.passed  # the median of the three runs is used, not the worst one
    over_target = benchmark.gate([result(3200)] * 3, baseline_of(3100, 3100, 3100, 3100, 3100))
    assert over_target.passed and over_target.targets_missed  # a target missed is reported...
    assert not benchmark.gate([result(3200)] * 3, baseline_of(3100), release=True).passed  # ...and fails a release
    assert baseline_of(3100).provisional


def test_cheaper_is_never_a_regression() -> None:
    assert benchmark.gate([result(3000, cost=100_000)] * 3, baseline_of(3000, 3000, 3000, 3000, 3000)).passed


def test_the_set_matches_beta_12() -> None:
    groups = benchmark.load_groups()
    assert len(groups) == 11 and sum(g["conversations"] for g in groups) == 200
    conversations = benchmark.load()
    by_group: dict[str, int] = {}
    for c in conversations:
        by_group[c.group] = by_group.get(c.group, 0) + 1
    planned = {g["id"]: g["conversations"] for g in groups if g["file"]}
    assert by_group == planned == {"product_questions": 40, "orders_payments": 30, "bursts": 15, "owner_commands": 20}
    assert len({c.id for c in conversations}) == len(conversations)  # ids are unique
    for c in conversations:  # every conversation ends on an outcome, never on a timing
        assert c.steps[-1].get("wait_for") and "ms" not in str(c.steps[-1]["wait_for"])


def test_shorthands_expand_to_scenario_steps() -> None:
    by_id = {c.id: c for c in benchmark.load()}
    assert by_id["pq-en-01"].steps == [
        {"actor": "c", "send_text": "How much is the chocolate truffle cake?"},
        {"wait_for": {"reply_contains": ["550", "950"]}, "timeout_s": benchmark.EXPECT_TIMEOUT_S},
    ]
    burst = by_id["bu-01"]
    assert [s.get("send_text") for s in burst.steps[:3]] == ["Hi", "do you have red velvet?", "how much for 1 kg"]
    owner = by_id["oc-04"]
    assert owner.owner and owner.scenario()["actors"] == {} and owner.steps[0]["actor"] == "owner"
