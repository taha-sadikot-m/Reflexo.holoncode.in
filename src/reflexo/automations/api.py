"""Public interface of the `automations` module. Other modules import only from here (POC_01 §4).

Automation definitions, validator, builder workflow, router, guards, instant/durable executors. Spec: POC_06.
"""

from reflexo.automations import builder, cel, demo, readback, runtime, validator
from reflexo.automations.guard import AutomationGuard
from reflexo.automations.router import INSTANT_QUEUE, ROUTE_QUEUE, AutomationRouter
from reflexo.automations.schema import AutomationDefinition, iso_seconds
from reflexo.automations.store import Installed, InvalidDefinition, activate, active_versions, install
from reflexo.common.contracts import EventRouterPort, JobHandlerRegistry, OrderGuardPort
from reflexo.common.ports import Registry

__all__ = [
    "AutomationDefinition",
    "AutomationGuard",
    "AutomationRouter",
    "Installed",
    "InvalidDefinition",
    "activate",
    "active_versions",
    "builder",
    "cel",
    "demo",
    "install",
    "iso_seconds",
    "readback",
    "register",
    "runtime",
    "validator",
]


def register(registry: Registry) -> None:
    registry.provide(OrderGuardPort, AutomationGuard())  # type: ignore[type-abstract]
    router = AutomationRouter()
    registry.provide(EventRouterPort, router)  # type: ignore[type-abstract]
    jobs = registry.optional(JobHandlerRegistry)  # type: ignore[type-abstract]
    if jobs is not None:
        jobs.register(ROUTE_QUEUE, router.route_job)
        jobs.register(INSTANT_QUEUE, router.instant_job)
