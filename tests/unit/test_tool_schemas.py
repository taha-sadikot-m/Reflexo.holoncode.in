"""Tool schemas sent to the model: refs inlined, metadata removed, property names kept (regression: Gemini 400
"requires unspecified property 'title'" when the schema cleaner dropped a property called `title`)."""

from __future__ import annotations

import json
from typing import Any

from reflexo.agent.tools import customer
from reflexo.agent.tools.base import TOOLS


def _walk(node: Any, path: str = "") -> list[str]:
    problems: list[str] = []
    if isinstance(node, dict):
        props = node.get("properties", {})
        for req in node.get("required", []):
            if req not in props:
                problems.append(f"{path}: required '{req}' missing from properties")
        for k, v in node.items():
            problems += _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            problems += _walk(v, f"{path}[{i}]")
    return problems


def test_all_customer_tool_schemas_are_consistent() -> None:
    for name in customer.CUSTOMER_TOOLS:
        schema = TOOLS[name].spec().parameters
        raw = json.dumps(schema)
        assert "$ref" not in raw and "$defs" not in raw and '"discriminator"' not in raw, name
        assert _walk(schema) == [], name


def test_option_title_property_survives() -> None:
    schema = TOOLS["reply"].spec().parameters
    raw = json.dumps(schema)
    assert '"title": {"maxLength": 80' in raw or '"title": {"type": "string"' in raw or '"title": {' in raw


def test_every_owner_tool_is_offered_to_the_owner_desk() -> None:
    """A tool tagged for the owner desk but missing from the list the desk binds is invisible to the model (a typed
    menu import once was): the tag and the list must agree."""
    import reflexo.agent.tools.customer  # noqa: F401  registers the customer tools too
    from reflexo.agent.tools.base import TOOLS
    from reflexo.agent.tools.owner import OWNER_TOOLS

    tagged = {name for name, t in TOOLS.items() if "owner" in t.desks}
    assert tagged == set(OWNER_TOOLS), (
        f"tagged, not offered: {tagged - set(OWNER_TOOLS)}; offered, not tagged: {set(OWNER_TOOLS) - tagged}"
    )
