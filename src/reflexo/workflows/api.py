"""Public interface of the `workflows` module. Other modules import only from here (POC_01 §4).

Temporal worker bootstrap and workflow/activity registry. Spec: POC_06 §7, POC_05 §3.1.
"""

from typing import Any
from uuid import UUID

from reflexo.common.contracts import DataRequestPort, WorkflowPort
from reflexo.common.enums import DataRequestKind
from reflexo.common.ports import Registry
from reflexo.workflows import privacy
from reflexo.workflows.client import Workflows, task_queue

__all__ = ["Workflows", "privacy", "register", "task_queue"]


class DataRequests:
    """DataRequestPort."""

    async def record_request(self, business_id: UUID, *, subject_type: str, subject_id: UUID, kind: str,
                             via: str | None = None, notes: str | None = None) -> UUID:  # fmt: skip
        subject: Any = subject_type
        return await privacy.record_request(
            business_id, subject_type=subject, subject_id=subject_id, kind=DataRequestKind(kind), via=via,
            notes=notes,
        )  # fmt: skip


def register(registry: Registry) -> None:
    registry.provide(WorkflowPort, Workflows())  # type: ignore[type-abstract]
    registry.provide(DataRequestPort, DataRequests())  # type: ignore[type-abstract]
