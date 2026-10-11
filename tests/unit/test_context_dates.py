"""The model gets the year and resolved relative dates (live bug: it wrote 2023-10-13 for "day after tomorrow")."""

from __future__ import annotations

from datetime import datetime

from reflexo.agent.context import _upcoming_days


def test_relative_days_are_resolved_with_year() -> None:
    line = _upcoming_days(datetime(2026, 10, 11, 14, 59))
    assert line.startswith("Dates: today = Sun 11 Oct (2026-10-11); tomorrow = Mon 12 Oct (2026-10-12); ")
    assert "day after tomorrow = Tue 13 Oct (2026-10-13)" in line
    assert "Saturday = Sat 17 Oct (2026-10-17)" in line and "(2026-10-18)" in line


def test_dates_roll_over_the_year() -> None:
    assert "tomorrow = Fri 01 Jan (2027-01-01)" in _upcoming_days(datetime(2026, 12, 31, 20, 0))
