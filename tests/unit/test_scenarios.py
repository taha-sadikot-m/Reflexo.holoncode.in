"""Lab scenario files are checked statically: a typo in a verb or condition fails here, not in front of a panel."""

from __future__ import annotations

import pytest

from reflexo.admin import scenarios, sim

VERBS = {"send_text", "tap_option", "pay_last_link", "owner_answer_last_question", "reply_with_last_short_code",
         "wait_for", "assert", "skip_waits", "chaos", "sleep_ms"}  # fmt: skip
CONDITIONS = {"reply_contains", "reply_has_options", "owner_message_contains", "order_status", "payment_status",
              "payment_link_created", "order_changed"}  # fmt: skip


@pytest.mark.parametrize("scenario", scenarios.load(), ids=lambda s: s["id"])
def test_scenario_is_well_formed(scenario: dict) -> None:  # type: ignore[type-arg]
    customers = {n for n, a in scenario.get("actors", {}).items() if a.get("role") == "customer"}
    assert customers, "a scenario needs at least one customer"
    for step in scenario["steps"]:
        verbs = [k for k in step if k not in ("actor", "timeout_s")]
        assert len(verbs) == 1 and verbs[0] in VERBS, step
        if step.get("actor"):
            assert step["actor"] in customers | {"owner"}, step
        if verbs[0] in ("wait_for", "assert"):
            assert set(step[verbs[0]]) <= CONDITIONS, step


def test_payload_helpers_read_what_a_person_sees() -> None:
    buttons = {"type": "interactive", "interactive": {"type": "button", "body": {"text": "Confirm?"},
               "action": {"buttons": [{"type": "reply", "reply": {"id": "apv:1:a", "title": "✅ Yes"}}]}}}  # fmt: skip
    assert sim.payload_text(buttons) == "Confirm?" and sim.payload_options(buttons) == [("apv:1:a", "✅ Yes")]
    assert sim.payload_text({"type": "text", "text": {"body": "hi"}}) == "hi"
