"""Automation language (POC_06 §1–4): schema, CEL, validator rules, executor choice and the read-back."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from reflexo.automations import cel, demo, readback, validator


def report(raw: dict[str, Any]) -> validator.ValidationReport:
    d, rep = validator.parse(raw)
    return rep if d is None else validator.validate(d)


def codes(rep: validator.ValidationReport) -> set[str]:
    return {f.code for f in [*rep.errors, *rep.owner_questions, *rep.warnings]}


def test_demo_definitions_are_valid_with_expected_executors() -> None:
    executors = []
    for raw in demo.ALL:
        d, _ = validator.parse(raw)
        assert d is not None and validator.validate(d).ok, raw["name"]
        executors.append(validator.compile_definition(d)[0])
    assert executors == ["guard", "durable", "durable", "durable", "instant", "instant"]


def test_trigger_index_normalizes_keywords() -> None:
    d, _ = validator.parse(demo.COMMENT_PRICE_DM)
    assert d is not None
    _, index = validator.compile_definition(d)
    assert (index.type, index.keywords, index.guard) == (
        "comment_received",
        ["cost", "kitne", "pp", "price", "rate"],
        False,
    )


def broken(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(demo.COD_CONFIRMATION)
    for path, value in changes.items():
        target: Any = raw
        *head, last = path.split("__")
        for key in head:
            target = target[int(key)] if key.isdigit() else target[key]
        target[int(last) if last.isdigit() else last] = value
    return raw


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (broken(steps__0__next="nowhere"), "V_SCHEMA"),
        (broken(steps__2__next="ask"), "V_CYCLE"),
        (broken(steps__1__on_timeout="end"), "V_UNREACHABLE"),
        (broken(steps__0__text="Order {{ order.nonexistent }}"), "V_TYPES"),
        (broken(steps__1__match="contains_any(event.text"), "V_TYPES"),
        (broken(steps__2__text="Thank you! ₹100 off next time"), "V_MONEY"),
        (broken(steps__1__timeout="2 hours"), "V_SCHEMA"),
        (broken(kind="guard"), "V_GUARD_SHAPE"),
    ],
)
def test_invalid_definitions_are_rejected(raw: dict[str, Any], code: str) -> None:
    rep = report(raw)
    assert code in codes(rep) and not rep.ok


def test_late_customer_message_without_template_becomes_an_owner_question() -> None:
    raw = copy.deepcopy(demo.DELIVERY_REMINDER)
    del raw["steps"][1]["template_fallback"]
    rep = report(raw)
    [q] = rep.owner_questions
    assert q.code == "V_WINDOW" and q.options and not rep.ok


def test_quiet_hours_warning_and_update_order_money_guard() -> None:
    raw = copy.deepcopy(demo.DELIVERY_REMINDER)
    raw["steps"][0]["until"] = "order.delivery_date.at('22:30') - duration('24h')"
    assert "V_QUIET" in codes(report(raw)) and report(raw).ok  # a warning, not a blocker
    raw = copy.deepcopy(demo.OUT_FOR_DELIVERY)
    raw["steps"][0] = {"id": "s", "type": "update_order", "fields": {"total_paise": 100}, "next": "e"}
    assert "V_MONEY" in codes(report(raw))


def test_cel_functions_and_templates() -> None:
    v = cel.sample_variables("order_confirmed")
    now = datetime(2026, 10, 11, tzinfo=UTC)
    assert cel.evaluate("order.has_category('cakes') && order.cake_count() == 1", v) is True
    assert cel.evaluate("contains_any(lower('HAAN ji'), ['haan'])", v) is True
    assert cel.evaluate("contains_any('shaandar', ['haan'])", v) is False  # whole words only
    assert cel.evaluate("order.delivery_date.at('10:00') - duration('24h')", v, now=now) == datetime(
        2026, 10, 14, 4, 30, tzinfo=UTC
    )  # 10:00 IST on 14 Oct
    assert (
        cel.render("{{ money(110000000) }} by {{ date_fmt(order.delivery_date, 'd MMM') }}", v)
        == "₹11,00,000 by 15 Oct"
    )
    with pytest.raises(cel.CelError):
        cel.evaluate("order.nope", v)
    with pytest.raises(cel.CelError, match="longer than"):
        cel.evaluate("1 + " * 200 + "1", v)


def test_readback_states_every_limit_wait_and_message() -> None:
    defs = [validator.parse(x)[0] for x in (demo.DAILY_CAKE_LIMIT, demo.COD_CONFIRMATION, demo.DELIVERY_REMINDER)]
    text = readback.readback([d for d in defs if d], "hi-Latn", demo_time_scale=60 / 86400)
    assert text.startswith("Samajh gayi, ye 3 rules lagaungi:")
    assert "maximum *8 cake* orders" in text and "9th order" in text and "agle available dates" in text
    assert "2 ghante tak customer ke reply ka intezaar" in text and "na aaye to: aapko bataungi" in text
    assert "delivery se ek din pehle 10:00 baje" in text and "template 'order_update'" in text
    assert "⏱ Demo mode: 1 din = 60 sec." in text
    en = readback.readback([d for d in defs if d], "en")
    assert "at most *8 cake* orders per delivery date" in en and "wait up to 2 hours" in en


def test_builder_never_trusts_model_template_fallbacks() -> None:
    """Live finding: Gemini wrote null/strings into template_fallback; the approved template is attached instead."""
    from reflexo.automations import builder

    for bad in (None, "order_update", {"name": "made_up", "vars": []}):
        raw = copy.deepcopy(demo.DELIVERY_REMINDER)
        raw["steps"][1]["template_fallback"] = bad
        [fixed], problems = builder.check([raw])
        assert problems == []
        assert fixed["steps"][1]["template_fallback"]["name"] == "order_update"
