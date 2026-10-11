"""Public interface of the `owner` module. Other modules import only from here (POC_01 §4).

Owner timeline, file library, notifications, onboarding flow, deterministic owner commands. Spec: POC_04 §7-9, POC_05 §3.4.
"""

from reflexo.common.contracts import OwnerCommandPort, OwnerNotifyPort
from reflexo.common.ports import Registry
from reflexo.owner.commands import OwnerCommands
from reflexo.owner.notify import OwnerNotifier

__all__ = ["OwnerCommands", "OwnerNotifier", "register"]


def register(registry: Registry) -> None:
    registry.provide(OwnerNotifyPort, OwnerNotifier())  # type: ignore[type-abstract]
    registry.provide(OwnerCommandPort, OwnerCommands(registry))  # type: ignore[type-abstract]
