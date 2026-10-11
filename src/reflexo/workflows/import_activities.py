"""`ImportWorkflow` activities (POC_05 §3.1). Each is idempotent per `import_run_id` (Temporal retries them)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError

import reflexo.approvals.api as approvals
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner
from reflexo.common.db import get_db
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)
SOURCE_KIND = {"image": "image", "pdf": "pdf"}


def _ids(inp: dict[str, Any]) -> tuple[UUID, UUID]:
    return UUID(inp["business_id"]), UUID(inp["import_run_id"])


async def _say(inp: dict[str, Any], key: str, blocks: list[dict[str, Any]]) -> None:
    business_id, run_id = _ids(inp)
    async with get_db().tenant_session(business_id) as s:
        await gateway.outbound().enqueue(
            s,
            business_id=business_id,
            conversation_id=UUID(inp["reply_conversation_id"]),
            blocks=blocks,
            origin="system",
            idempotency_base=f"import:{run_id}:{key}",
        )


async def _file(inp: dict[str, Any]) -> owner.OwnerFile:
    f = await owner.get_file(UUID(inp["business_id"]), UUID(inp["owner_file_id"]))
    if f is None:
        raise ApplicationError("That file is no longer available.", type="ImportFailed", non_retryable=True)
    return f


def _source_kind(f: owner.OwnerFile, mime: str) -> str:
    if f.kind == "spreadsheet":
        return "csv" if mime.startswith("text/csv") or (f.filename or "").lower().endswith(".csv") else "excel"
    if f.kind in SOURCE_KIND:
        return SOURCE_KIND[f.kind]
    raise ApplicationError(
        "I can read menus from photos, PDFs, Excel or CSV files.", type="ImportFailed", non_retryable=True
    )


@activity.defn(name="import.start")
async def import_start(inp: dict[str, Any]) -> dict[str, Any]:
    business_id, run_id = _ids(inp)
    f = await _file(inp)
    found = await gateway.media_path(f.media_object_id)
    if found is None:
        raise ApplicationError("That file is no longer available.", type="ImportFailed", non_retryable=True)
    kind = _source_kind(f, found[1])
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""INSERT INTO import_runs (id, business_id, source_kind, source_file_id, status, workflow_id)
                    VALUES (:id, :b, :k, :f, 'extracting', :w) ON CONFLICT (id) DO NOTHING"""),
            {"id": run_id, "b": business_id, "k": kind, "f": f.id, "w": f"import-{run_id}"},
        )
    if inp.get("mode") == "onboarding":
        await owner.onboarding.set_state(business_id, "extracting", {"import_run_id": str(run_id)})
    if inp.get("announce", False):  # started by the owner desk, which already said "reading…"
        await _say(inp, "start", [{"type": "text", "text": f"Reading your file… 📄 ({f.filename or kind})"}])
    await emit("import", PipelineEventStatus.START, business_id=business_id, import_run=str(run_id), kind=kind)
    return {"kind": kind, "filename": f.filename}


@activity.defn(name="import.extract")
async def import_extract(inp: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]]:
    business_id, _ = _ids(inp)
    f = await _file(inp)
    found = await gateway.media_path(f.media_object_id)
    assert found is not None
    path, mime = found
    content = await asyncio.to_thread(Path(path).read_bytes)
    try:
        if meta["kind"] in ("excel", "csv"):
            items = await knowledge.imports.extract_spreadsheet(business_id, content, mime, f.filename)
        else:
            items = await knowledge.imports.extract_visual(business_id, content, mime)
    except knowledge.imports.ImportFailed as exc:
        raise ApplicationError(str(exc), type="ImportFailed", non_retryable=True) from exc
    if not items:
        raise ApplicationError(
            "I couldn't find any items with prices in this file.", type="ImportFailed", non_retryable=True
        )
    return [i.model_dump() for i in items]


@activity.defn(name="import.prepare")
async def import_prepare(inp: dict[str, Any], meta: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    business_id, run_id = _ids(inp)
    return await knowledge.imports.prepare(
        business_id,
        run_id,
        [knowledge.imports.ExtractedItem.model_validate(i) for i in items],
        visual=meta["kind"] in ("image", "pdf"),
        origin="onboarding" if inp["mode"] == "onboarding" else "import",
        member_id=UUID(inp["member_id"]) if inp.get("member_id") else None,
        filename=meta.get("filename"),
    )


@activity.defn(name="import.ask_confirmation")
async def import_ask(inp: dict[str, Any], meta: dict[str, Any], prepared: dict[str, Any]) -> str:
    business_id, run_id = _ids(inp)
    async with get_db().tenant_session(business_id) as s:
        existing = (
            await s.execute(
                text("""SELECT id, short_code FROM approval_requests WHERE status = 'pending'
                        AND subject->>'import_run_id' = :r LIMIT 1"""),
                {"r": str(run_id)},
            )
        ).first()
        if existing is not None:  # a retried activity: same approval, same code
            code, approval_id = existing.short_code, existing.id
        else:
            approval = await approvals.create(
                s,
                business_id,
                "change_confirmation",
                {
                    "change_set_id": prepared["change_set_id"],
                    "import_run_id": str(run_id),
                    "workflow_id": f"import-{run_id}",
                    "owner_conversation_id": inp["reply_conversation_id"],
                },
                {"summary": f"Import {meta.get('filename') or ''}".strip(), "lines": prepared["lines"]},
                requested_by=f"import:{run_id}",
            )
            code, approval_id = approval.short_code, approval.id
        await s.execute(text("UPDATE import_runs SET status = 'review' WHERE id = :r"), {"r": run_id})
    body = knowledge.imports.summary_text(meta.get("filename"), prepared, code)
    buttons = [{"id": f"apv:{approval_id}:a", "title": "✅ Apply"}, {"id": f"apv:{approval_id}:r", "title": "✖ Cancel"}]
    blocks: list[dict[str, Any]] = (
        [{"type": "buttons", "text": body, "options": buttons}]
        if len(body) <= 1024
        else [{"type": "text", "text": body}, {"type": "buttons", "text": "Apply?", "options": buttons}]
    )
    await _say(inp, "summary", blocks)
    await emit(
        "import",
        PipelineEventStatus.OK,
        business_id=business_id,
        import_run=str(run_id),
        step="review",
        **prepared["stats"],
    )
    return str(code)


@activity.defn(name="import.apply")
async def import_apply(inp: dict[str, Any], prepared: dict[str, Any]) -> None:
    business_id, run_id = _ids(inp)
    try:
        await knowledge.apply(business_id, UUID(prepared["change_set_id"]))
    except knowledge.ChangeConflict as exc:
        raise ApplicationError(
            "Some items changed while you were reviewing; nothing was applied. Send the file again.",
            type="ImportFailed",
            non_retryable=True,
        ) from exc
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("UPDATE import_runs SET status = 'committed', finished_at = now() WHERE id = :r"), {"r": run_id}
        )
    n = len(prepared["lines"])
    await _say(
        inp,
        "applied",
        [{"type": "text", "text": f"Done ✅ {n} change{'s' if n != 1 else ''} applied. (/undo to revert)"}],
    )
    await emit("import", PipelineEventStatus.OK, business_id=business_id, import_run=str(run_id), step="committed")


@activity.defn(name="import.finish")
async def import_finish(inp: dict[str, Any], status: str, message: str) -> None:
    """End without applying: failed / cancelled / nothing to change."""
    business_id, run_id = _ids(inp)
    async with get_db().tenant_session(business_id) as s:
        await s.execute(
            text("""UPDATE import_runs SET status = CAST(:st AS import_status), error = :e, finished_at = now()
                    WHERE id = :r AND status NOT IN ('committed')"""),
            {"st": status, "e": message if status == "failed" else None, "r": run_id},
        )
        await s.execute(
            text("""UPDATE approval_requests SET status = 'cancelled'
                    WHERE status = 'pending' AND subject->>'import_run_id' = :r"""),
            {"r": str(run_id)},
        )
    await _say(inp, f"end:{status}", [{"type": "text", "text": message}])
    await emit(
        "import",
        PipelineEventStatus.SKIP if status != "failed" else PipelineEventStatus.ERROR,
        business_id=business_id,
        import_run=str(run_id),
        step=status,
    )


ACTIVITIES: list[Callable[..., Any]] = [
    import_start,
    import_extract,
    import_prepare,
    import_ask,
    import_apply,
    import_finish,
]
