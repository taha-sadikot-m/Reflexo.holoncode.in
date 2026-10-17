"""Invalid tool arguments are explained in full, with the shape each value should have (WP-00).

Live 14 Oct (traces 08d5f4ff and 279be1f6): after `order_start` the model sent a buttons block in a shape of its
own, twice. The repair named one missing field without the shape, the model sent the same arguments again, and the
customer got the holding message.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from reflexo.agent.schemas import AgentOutput
from reflexo.agent.tools.base import explain_invalid
from reflexo.agent.tools.orders import OrderStartArgs

ORDER = "01a1396e-f375-708b-9963-f0b0f8fa680c"
LIVE_REPLY: dict[str, Any] = {  # the stored arguments of the live reply, customer words shortened
    "action": "send",
    "topic": "booking_appointments",
    "language": "en",
    "blocks": [
        {"type": "text", "text": "Got it! One 1 kg Chocolate Truffle Cake, total ₹1,000."},
        {
            "type": "buttons",
            "title": "Does that sound right? How would you like to pay?",
            "buttons": [{"text": "Pay online", "id": "razorpay_link"}, {"text": "Cash on Delivery", "id": "cod"}],
        },
    ],
    "referenced_records": [{"type": "order", "id": ORDER}],
}


def explain(model: Any, args: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(args)
    return explain_invalid(model, exc.value, args)


def test_live_buttons_reply_gets_every_problem_and_the_expected_shape() -> None:
    [line] = explain(AgentOutput, LIVE_REPLY).splitlines()
    assert line.startswith('- blocks[1] (type "buttons"):')
    assert "missing text (string)" in line
    assert "missing options ([{id: string, title: string, aliases?: [string]}])" in line
    assert "not fields here: buttons, title" in line


def test_missing_top_level_field_shows_its_allowed_values() -> None:
    args = {k: v for k, v in LIVE_REPLY.items() if k != "topic"} | {"blocks": LIVE_REPLY["blocks"][:1]}
    [line] = explain(AgentOutput, args).splitlines()
    assert line.startswith('- arguments: missing topic ("greeting_smalltalk" | ')
    assert '"order_status"' in line


def test_wrong_value_and_unknown_block_type_are_explained() -> None:
    args = LIVE_REPLY | {"action": "reply", "blocks": [{"type": "card", "text": "x"}]}
    lines = explain(AgentOutput, args).splitlines()
    assert any(ln.startswith("- action:") and '"send" | "ask_owner" | "handoff" | "no_reply"' in ln for ln in lines)
    assert any(ln.startswith("- blocks[0]:") and "'buttons'" in ln for ln in lines)


def test_every_tool_gets_the_same_explanation() -> None:
    [line] = explain(OrderStartArgs, {"items": [{"variant_id": "v", "quantity": 1}]}).splitlines()
    assert line == "- items[0]: missing offering_id (string)"
