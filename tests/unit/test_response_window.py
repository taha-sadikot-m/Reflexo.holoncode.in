"""Demo time compresses calendar time, not a person's reaction time (live 12 Oct: 2 h became 5 s)."""

from __future__ import annotations

from reflexo.workflows.automation_run import response_window

DEMO = 60 / 86400  # 1 day = 60 s


def test_scaled_window_never_drops_below_the_floor() -> None:
    assert response_window(7200, DEMO, 90) == 90  # PT2H → 5 s scaled → 90 s floor


def test_floor_never_exceeds_the_real_window() -> None:
    assert response_window(30, DEMO, 90) == 30


def test_long_windows_stay_scaled_above_the_floor() -> None:
    assert response_window(7 * 86400, DEMO, 90) == 7 * 60  # a week → 7 min


def test_real_time_is_untouched() -> None:
    assert response_window(7200, 1.0, 90) == 7200
    assert response_window(7200, DEMO, 0) == 7200 * DEMO
