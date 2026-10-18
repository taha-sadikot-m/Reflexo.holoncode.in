"""The Cedar schema and the repository layers (BETA_04 §7.2, §7.3).

`ACTIONS` is read from the schema text with a plain parser of the `action ... in [Action::"<risk>"] appliesTo`
lines, independently of Cedar: the reference evaluator uses it, so the differential test also checks that both
sides agree on every action's risk class and resource type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import cedarpy

    CEDAR_LOADED = True
except ImportError:  # the process refuses to start unless the reference evaluator is chosen (api.start)
    cedarpy = None  # type: ignore[assignment]
    CEDAR_LOADED = False

from reflexo.common.config import get_settings

RISKS = ("routine", "commitment", "money")
FILE_LAYERS = {"platform": "platform.cedar", "default": "defaults.cedar", "ceiling": "ceilings.cedar"}
SCHEMA_FILE = "reflexo.cedarschema"
# a stand-in connector action, so that policies about connector tools validate before any tool is approved
PROBE_CONNECTOR_ACTION = (
    'action "connector.probe.write" in [Action::"commitment"] '
    "appliesTo { principal: [Agent], resource: [ConnectorTool], context: Ctx };"
)
_ACTION = re.compile(
    r'action\s+((?:"[^"]+"\s*,?\s*)+)\s+in\s+\[Action::"(\w+)"\]\s+appliesTo\s*\{\s*principal:\s*\[([^\]]*)\],'
    r"\s*resource:\s*\[([^\]]*)\]"
)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    risk: str
    principals: tuple[str, ...]
    resources: tuple[str, ...]


class PolicyConfigError(RuntimeError):
    pass


def policy_dir() -> Path:
    return Path(get_settings().policy_dir)


@lru_cache(maxsize=1)
def schema_text() -> str:
    return (policy_dir() / SCHEMA_FILE).read_text(encoding="utf-8")


def parse_actions(text: str) -> dict[str, ActionSpec]:
    body = re.sub(r"//[^\n]*", "", text)
    out: dict[str, ActionSpec] = {}
    for m in _ACTION.finditer(body):
        principals = tuple(p.strip() for p in m.group(3).split(",") if p.strip())
        resources = tuple(r.strip() for r in m.group(4).split(",") if r.strip())
        for name in re.findall(r'"([^"]+)"', m.group(1)):
            out[name] = ActionSpec(name, m.group(2), principals, resources)
    return out


@lru_cache(maxsize=1)
def actions() -> dict[str, ActionSpec]:
    return parse_actions(schema_text())


def full_schema(fragment: str = "") -> str:
    """The schema with a business's generated connector actions inside the namespace."""
    if not fragment.strip():
        return schema_text()
    text = schema_text().rstrip()
    if not text.endswith("}"):
        raise PolicyConfigError("the schema must end with the namespace's closing brace")
    return f"{text[:-1].rstrip()}\n{fragment.strip()}\n}}\n"


@lru_cache(maxsize=64)
def schema_handle(fragment: str = "") -> Any:
    try:
        return cedarpy.Schema.from_str(full_schema(fragment))
    except ValueError as exc:
        raise PolicyConfigError(f"the Cedar schema does not parse: {exc}") from exc


@lru_cache(maxsize=8)
def layer_text(layer: str) -> str:
    return (policy_dir() / FILE_LAYERS[layer]).read_text(encoding="utf-8")


def validate(policies: str, fragment: str = "") -> list[str]:
    """Cedar's strict validation of a policy set against the schema; [] = valid."""
    if not policies.strip():
        return []
    result = cedarpy.validate_policies(policies, schema_handle(fragment))
    return [str(e) for e in result.errors]


def check_files() -> None:
    """At start: the schema parses and every repository layer validates (the ceilings with a stand-in connector
    action, as their connector policy only applies once a tool exists)."""
    schema_handle()
    for layer in FILE_LAYERS:
        errors = validate(layer_text(layer), PROBE_CONNECTOR_ACTION)
        if errors:
            raise PolicyConfigError(f"config/policy/{FILE_LAYERS[layer]} is invalid: {'; '.join(errors)}")


def forget() -> None:
    for f in (schema_text, actions, schema_handle, layer_text):
        f.cache_clear()
