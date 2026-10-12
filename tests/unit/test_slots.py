"""Delivery slot matching by time, not by spelling (live 12 Oct: "16:00-20:00" was refused for the "4pm-8pm" slot
and the customer was asked three times to pick the slot they had already named)."""

from __future__ import annotations

import pytest

from reflexo.commerce.orders import _slot_for_time

OPTIONS = ["10am-1pm", "1pm-4pm", "4pm-8pm"]


@pytest.mark.parametrize(
    ("value", "slot"),
    [
        ("16:00-20:00", "4pm-8pm"),
        ("4-8pm", "4pm-8pm"),
        ("4 to 8 pm", "4pm-8pm"),
        ("4-8", "4pm-8pm"),  # no am/pm: only the pm reading is a slot
        ("5pm", "4pm-8pm"),
        ("17:30", "4pm-8pm"),
        ("4pm", "4pm-8pm"),  # a slot owns its start time
        ("10-1pm", "10am-1pm"),
        ("11am", "10am-1pm"),
        ("13:00-16:00", "1pm-4pm"),
        ("2-3pm", "1pm-4pm"),
        ("12-4pm", None),  # spans two slots: ask
        ("9pm", None),  # outside every slot
        ("evening", None),  # words are the day-part fallback's job
    ],
)
def test_slot_for_time(value: str, slot: str | None) -> None:
    assert _slot_for_time(value, OPTIONS) == slot
