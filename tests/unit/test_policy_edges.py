"""The policy engine's edges (safety core, BETA_08 §2.4): the checker bridge when the binary misbehaves, compiler
corner cases, an erroring policy, rule validation, schema fragments and broken layer files."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import reflexo.policy.api as policy
from reflexo.common.config import get_settings
from reflexo.policy import check, compile, schema
from reflexo.policy.evaluate import LayerSets, evaluate, load
from tests.policy.gen import BUSINESS, CUSTOMERS
from tests.policy.test_layers import order_request

PY = shlex.quote(sys.executable)


@pytest.fixture
def checker(monkeypatch: pytest.MonkeyPatch) -> Any:
    def use(script: str, timeout: float = 5.0) -> None:
        monkeypatch.setenv("REFLEXO_POLICY_CHECK_CMD", f"{PY} -c {shlex.quote(script)}")
        monkeypatch.setenv("REFLEXO_POLICY_CHECK_TIMEOUT_S", str(timeout))
        get_settings.cache_clear()

    yield use
    monkeypatch.undo()
    get_settings.cache_clear()


async def test_a_checker_that_answers_is_available(checker: Any) -> None:
    checker('import json, sys; sys.stdin.read(); print(json.dumps({"may_error": []}))')
    report = await policy.check("never-errors", set="")
    assert report.available and report.clean


@pytest.mark.parametrize(
    ("script", "limit_s", "error"),
    [
        ("import time; time.sleep(5)", 0.5, "timeout"),
        ("import sys; sys.exit(3)", 5.0, "exit 3"),
        ("print('not json')", 5.0, "unreadable output"),
    ],
)
async def test_a_misbehaving_checker_is_unavailable_never_an_error(
    checker: Any, script: str, limit_s: float, error: str
) -> None:
    checker(script, limit_s)
    report = await policy.check("lint", set="")
    assert (report.available, report.error, report.clean) == (False, error, False)


async def test_a_missing_checker_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLEXO_POLICY_CHECK_CMD", "reflexo-policy-check-not-installed-anywhere")
    get_settings.cache_clear()
    try:
        report = await policy.check("lint", set="")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert not report.available and str(report.error).startswith("not runnable")


def test_an_invalid_input_report_is_not_clean() -> None:
    assert not check.CheckReport(command="lint", available=True, error="set does not parse").clean


def test_strings_escape_every_control_character() -> None:
    assert compile.cedar_str("a\rb\tc\x7f") == '"a\\rb\\tc\\u{7f}"'


def test_the_compiler_refuses_unknown_capabilities_and_handles_connector_tools() -> None:
    with pytest.raises(policy.PolicyConfigError):
        compile.resource_type("order.teleport")
    assert compile.resource_type("connector.notion.update", ["connector.notion.update"]) == "ConnectorTool"
    rule = policy.Rule(id=UUID(int=1), capability="connector.notion.update", effect="propose", says="ask me")
    text = compile.rule_policy(rule, ["connector.notion.update"])
    assert 'action == Reflexo::Action::"connector.notion.update"' in text


def test_fields_a_resource_does_not_have_never_hold() -> None:
    status = policy.Condition(field="order.status", op="is", value="draft")
    assert compile.condition_expr(status, "Customer") == "false"
    count = policy.Condition(field="customer.orders_count", op="gt", value=1)
    assert compile.condition_expr(count, "Business") == "false"
    scoped = policy.Rule(id=UUID(int=2), capability="slot.find", effect="deny", says="no", scope="customer",
                         customer_id=CUSTOMERS[0])  # fmt: skip
    assert compile.scope_expr(scoped, "Business") == "false"


def test_a_fractional_equality_compiles_to_a_constant() -> None:
    total_is = policy.Condition(field="order.total_rupees", op="is", value=499.5)
    total_is_not = policy.Condition(field="order.total_rupees", op="is_not", value=499.5)
    hours_is_not = policy.Condition(field="order.hours_until_delivery", op="is_not", value=2.5)
    assert compile.condition_expr(total_is, "Order") == "false"
    assert compile.condition_expr(total_is_not, "Order") == "true"
    assert compile.condition_expr(hours_is_not, "Order") == "context has hours_until_delivery"


def test_a_policy_that_errors_never_lets_an_action_through_unasked() -> None:
    shop = load(
        "shop",
        '@id("rule:a") @effect("allow")\npermit (principal, action == Reflexo::Action::"order.cancel", resource);\n'
        '@id("rule:b") @effect("allow")\npermit (principal, action == Reflexo::Action::"order.cancel", resource)\n'
        "  when { context.hours_since_placed > 1 };\n",  # absent from the context: an evaluation error
    )
    sets = LayerSets(load("customer", ""), shop)
    assert sets.platform.layer == "platform" and sets.default.layer == "default" and sets.ceiling.layer == "ceiling"
    d = evaluate(order_request(requested_by="owner_turn"), sets)
    assert d.effect == "propose"


def test_requests_find_their_entities() -> None:
    req = order_request()
    assert req.entity(None) is None
    assert req.entity(req.resource)["uid"] == req.resource.uid  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        ("order.status", "lt", "draft"),  # an operator the type does not have
        ("order.total_rupees", "lt", ["1", "2"]),
        ("order.total_rupees", "lt", "lots"),
        ("order.total_rupees", "gt", 10**13),
        ("order.categories", "includes", ["cakes"]),
        ("order.categories", "includes", " "),
        ("order.status", "is", ["draft", "confirmed"]),
        ("order.status", "in", ["shipped"]),  # not a value the field has
    ],
)
def test_ill_typed_conditions_are_refused_when_written(field: str, op: str, value: Any) -> None:
    with pytest.raises(ValueError):
        policy.Condition(field=field, op=op, value=value)  # type: ignore[arg-type]


def test_a_number_condition_on_a_value_that_is_not_a_number_does_not_hold() -> None:
    assert not policy.Condition(field="order.total_rupees", op="gt", value=1).holds({"order.total_rupees": "n/a"})


def test_schema_fragments_and_broken_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "connector.probe.write" in schema.full_schema(schema.PROBE_CONNECTOR_ACTION)
    assert schema.validate("") == []
    with pytest.raises(policy.PolicyConfigError):
        schema.schema_handle('action "x" appliesTo { nonsense')
    for name in (schema.SCHEMA_FILE, *schema.FILE_LAYERS.values()):
        source = Path(get_settings().policy_dir) / name
        (tmp_path / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "defaults.cedar").write_text(
        'permit (principal, action == Reflexo::Action::"nope", resource);', "utf-8"
    )
    monkeypatch.setenv("REFLEXO_POLICY_DIR", str(tmp_path))
    get_settings.cache_clear()
    schema.forget()
    try:
        with pytest.raises(policy.PolicyConfigError, match=r"defaults\.cedar"):
            schema.check_files()
        (tmp_path / schema.SCHEMA_FILE).write_text("namespace Reflexo { entity Business; ", encoding="utf-8")
        schema.forget()
        with pytest.raises(policy.PolicyConfigError, match="closing brace"):
            schema.full_schema('action "x";')
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        schema.forget()


def test_the_reference_engine_is_used_only_when_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    req = order_request(status="draft")
    monkeypatch.setenv("REFLEXO_POLICY_ENGINE", "reference")
    get_settings.cache_clear()
    try:
        assert policy.evaluate(req, policy.EMPTY, []) == policy.reference.evaluate(req, [])
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert policy.evaluate(req, policy.EMPTY).effect == "act"


def test_an_example_on_a_customer_resource_keeps_order_fields_out() -> None:
    agent, aent = policy.agent_entity(BUSINESS, "customer")
    cref, cent = policy.customer_entity(BUSINESS, CUSTOMERS[0], 3)
    req = policy.PolicyRequest(
        principal=agent, action="message.send", resource=cref, entities=[aent, cent],
        context={"requested_by": "automation", "owner_approved": False},
    )  # fmt: skip
    assert check._set_field(req, "order.status", "draft") is None
    assert check._set_field(req, "customer.orders_count", 0) is not None
