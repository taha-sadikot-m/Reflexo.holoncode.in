"""Public interface of the `workflows` module. Other modules import only from here (POC_01 §4).

Temporal worker bootstrap and workflow/activity registry. Spec: POC_06 §7, POC_05 §3.1.
"""

from reflexo.common.contracts import WorkflowPort
from reflexo.common.ports import Registry
from reflexo.workflows.client import Workflows, task_queue

__all__ = ["Workflows", "register", "task_queue"]


def register(registry: Registry) -> None:
    registry.provide(WorkflowPort, Workflows())  # type: ignore[type-abstract]
