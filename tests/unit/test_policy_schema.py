"""The Cedar schema against the capabilities in code (CHK-POL-8), the repository layers, and engine selection."""

from __future__ import annotations

import pytest

import reflexo.policy.api as policy
from reflexo.agent.capabilities import CAPABILITIES
from reflexo.policy import schema


@pytest.mark.check("CHK-POL-8")
def test_every_capability_has_a_schema_action_and_every_action_a_capability() -> None:
    actions = policy.actions()
    assert set(CAPABILITIES) == set(actions)  # reflexo.domains joins this check when WP-27 builds its registry
    for name, cap in CAPABILITIES.items():
        assert (cap.risk, (cap.resource,)) == (actions[name].risk, actions[name].resources), name
        assert cap.label and cap.verb  # the owner can read every capability


def test_the_repository_layers_validate_against_the_schema() -> None:
    policy.check_files()
    assert set(policy.file_set("platform").annotations) == {"policy0", "policy1", "policy2", "policy3"}


def test_the_schema_parser_reads_groups_resources_and_principals() -> None:
    parsed = schema.parse_actions(
        'action "a.b", "a.c" in [Action::"money"] appliesTo { principal: [Agent, Member], resource: [Order], '
        "context: Ctx };"
    )
    assert parsed["a.c"] == schema.ActionSpec("a.c", "money", ("Agent", "Member"), ("Order",))


def test_cedar_is_required_unless_the_reference_engine_is_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    from reflexo.common.config import get_settings

    monkeypatch.setattr("reflexo.policy.api.CEDAR_LOADED", False)
    with pytest.raises(policy.PolicyConfigError, match="REFLEXO_POLICY_ENGINE=reference"):
        policy.start()  # no silent fallback
    monkeypatch.setenv("REFLEXO_POLICY_ENGINE", "reference")
    get_settings.cache_clear()
    try:
        policy.start()
    finally:
        monkeypatch.delenv("REFLEXO_POLICY_ENGINE")
        get_settings.cache_clear()


def test_strings_are_cedar_literals() -> None:
    assert policy.cedar_str('say "no"\\\n\x01') == '"say \\"no\\"\\\\\\n\\u{1}"'
