"""Public interface of the `automations` module. Other modules import only from here (POC_01 §4).

Automation definitions, validator, builder workflow, router, guards, instant/durable executors. Spec: POC_06.
"""

from reflexo.automations import cel, demo, readback, validator
from reflexo.automations.guard import AutomationGuard
from reflexo.automations.schema import AutomationDefinition
from reflexo.automations.store import Installed, InvalidDefinition, activate, active_versions, install
from reflexo.common.contracts import OrderGuardPort
from reflexo.common.ports import Registry

__all__ = [
    "AutomationDefinition",
    "AutomationGuard",
    "Installed",
    "InvalidDefinition",
    "activate",
    "active_versions",
    "cel",
    "demo",
    "install",
    "readback",
    "register",
    "validator",
]


def register(registry: Registry) -> None:
    registry.provide(OrderGuardPort, AutomationGuard())  # type: ignore[type-abstract]
