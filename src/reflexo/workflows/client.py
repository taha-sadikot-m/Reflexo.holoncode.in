"""Temporal client (WorkflowPort): start workflows and deliver signals from any process (API, worker, voice)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from reflexo.common.config import get_settings
from reflexo.common.ids import new_id
from reflexo.common.logging import get_logger

log = get_logger(__name__)
_client: Client | None = None
_lock = asyncio.Lock()


def task_queue() -> str:
    return get_settings().temporal_task_queue


async def client() -> Client:
    global _client
    async with _lock:
        if _client is None:
            s = get_settings()
            _client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    return _client


class Workflows:
    async def start_import(
        self,
        business_id: UUID,
        owner_file_id: UUID,
        *,
        mode: str,
        member_id: UUID | None,
        reply_conversation_id: UUID,
    ) -> UUID:
        run_id = new_id()
        c = await client()
        await c.start_workflow(
            "ImportWorkflow",
            {
                "import_run_id": str(run_id),
                "business_id": str(business_id),
                "owner_file_id": str(owner_file_id),
                "mode": mode,
                "member_id": str(member_id) if member_id else None,
                "reply_conversation_id": str(reply_conversation_id),
            },
            id=f"import-{run_id}",
            task_queue=get_settings().temporal_task_queue,
        )
        log.info("import_started", business_id=str(business_id), import_run_id=str(run_id), mode=mode)
        return run_id

    async def signal(self, workflow_id: str, name: str, payload: dict[str, Any]) -> bool:
        c = await client()
        try:
            await c.get_workflow_handle(workflow_id).signal(name, payload)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                log.info("workflow_signal_target_gone", workflow_id=workflow_id, signal=name)
                return False
            raise
        return True
