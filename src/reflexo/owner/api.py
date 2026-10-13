"""Public interface of the `owner` module. Other modules import only from here (POC_01 §4).

Owner timeline, file library, notifications, onboarding flow, deterministic owner commands. Spec: POC_04 §7-9, POC_05 §3.4.
"""

from reflexo.common.contracts import OwnerCommandPort, OwnerNotifyPort
from reflexo.common.ports import Registry
from reflexo.owner import onboarding
from reflexo.owner.commands import OwnerCommands, execute_decision
from reflexo.owner.files import (
    TYPED_SOURCE_MIME,
    OwnerFile,
    get_file,
    member_info,
    save_typed_source,
    search_files,
    set_description,
)
from reflexo.owner.notify import OwnerNotifier

__all__ = [
    "TYPED_SOURCE_MIME",
    "OwnerCommands",
    "OwnerFile",
    "OwnerNotifier",
    "execute_decision",
    "get_file",
    "member_info",
    "onboarding",
    "register",
    "save_typed_source",
    "search_files",
    "set_description",
]


def register(registry: Registry) -> None:
    registry.provide(OwnerNotifyPort, OwnerNotifier())  # type: ignore[type-abstract]
    registry.provide(OwnerCommandPort, OwnerCommands(registry))  # type: ignore[type-abstract]
