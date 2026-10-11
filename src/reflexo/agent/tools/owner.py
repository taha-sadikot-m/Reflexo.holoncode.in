"""Owner desk tools (POC_04 §3.3, F04/F05/F07). Read-back is mandatory for every write:

* `catalog_change` / `knowledge_change` build a *draft* change set (or a settings patch) plus an
  `approval_requests(kind='change_confirmation')` and return the read-back lines and a 4-digit code;
  nothing is applied until the owner confirms (YES 1234 / button / "yes").
* `send_to_customer` drafts an `outbound_draft` approval unless the owner said "send now".
* `undo` reverts the last applied change set immediately and reports what it undid.
The desk turns any approval created in the turn into [Confirm] [Cancel] buttons on the reply.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import reflexo.approvals.api as approvals
import reflexo.gateway.api as gateway
import reflexo.knowledge.api as knowledge
import reflexo.owner.api as owner
from reflexo.agent.tools.base import ToolContext, ToolError, tool
from reflexo.common import ports
from reflexo.common.contracts import QuestionAnswerPort, WorkflowPort
from reflexo.common.db import get_db
from reflexo.common.money import format_inr
from reflexo.common.timeutil import to_business_tz, utcnow

OWNER = {"owner"}


def _paise(rupees: float | int | str) -> int:
    try:
        value = round(float(str(rupees).replace(",", "").replace("₹", "").strip()) * 100)
    except ValueError as exc:
        raise ToolError(
            "VALIDATION_FAILED", f"bad price {rupees!r}", guidance="Pass the price in rupees, e.g. 999."
        ) from exc
    if value <= 0:
        raise ToolError("VALIDATION_FAILED", "price must be positive", guidance="Ask the owner for the price.")
    return value


async def _confirmation(
    ctx: ToolContext, subject: dict[str, Any], summary: str, lines: list[str], kind: str = "change_confirmation"
) -> dict[str, Any]:
    async with get_db().tenant_session(ctx.business_id) as s:
        approval = await approvals.create(
            s,
            ctx.business_id,
            kind,
            {**subject, "owner_conversation_id": str(ctx.conversation_id)},
            {"summary": summary, "lines": lines},
            requested_by=f"owner_desk:{ctx.member_id}",
        )
    return {
        "read_back": lines,
        "summary": summary,
        "approval": {"id": str(approval.id), "code": approval.short_code},
        "next": f"Show the read-back lines exactly and ask the owner to confirm (YES {approval.short_code}). "
        "Nothing is changed until they confirm.",
    }


# ---------------------------------------------------------------- catalogue


class CatalogOp(BaseModel):
    action: Literal["add", "update_price", "update", "hide", "show"]
    target: str = Field(description="Product name as the owner said it, incl. size if relevant ('truffle 1 kg')")
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="add: {name, category, price} or {name, category, variants:[{name, price}]}; "
        "update_price: {price} (rupees); update: {description|category|name}",
    )


class CatalogChangeArgs(BaseModel):
    ops: list[CatalogOp] = Field(min_length=1, max_length=20)


async def _resolve(ctx: ToolContext, target: str) -> knowledge.OfferingRef:
    found = await knowledge.resolve_offerings(ctx.business_id, target)
    if not found:
        raise ToolError("NOT_FOUND", f"no product matches '{target}'", guidance="Ask the owner which product.")
    best = found[0]
    close = [f for f in found if f.score >= best.score - 0.05]
    if len(close) > 1 and best.name.lower() not in target.lower():
        names = ", ".join(f.name for f in close[:4])
        raise ToolError("AMBIGUOUS_MATCH", f"'{target}' matches several products", guidance=f"Ask which one: {names}.")
    return best


@tool("catalog_change", CatalogChangeArgs, desks=OWNER, side_effect=True)
async def catalog_change(ctx: ToolContext, args: CatalogChangeArgs, s: AsyncSession) -> dict[str, Any]:
    """Add products, change prices or details, hide/show products. Builds a read-back for the owner to confirm;
    nothing changes until they confirm."""
    cs = await knowledge.begin(ctx.business_id, "owner_command", "Catalogue change", member_id=ctx.member_id)
    for op in args.ops:
        if op.action == "add":
            name = str(op.fields.get("name") or op.target).strip()
            oid = await knowledge.add(
                cs,
                "offering",
                "create",
                None,
                {
                    "name": name,
                    "category": str(op.fields.get("category") or "other"),
                    "description": str(op.fields.get("description") or ""),
                    "base_price_paise": _paise(op.fields["price"]) if op.fields.get("price") else None,
                },
            )
            assert oid is not None
            for v in op.fields.get("variants") or []:
                await knowledge.add(
                    cs,
                    "variant",
                    "create",
                    None,
                    {"offering_id": str(oid.entity_id), "name": str(v["name"]), "price_paise": _paise(v["price"])},
                )
            continue
        offering = await _resolve(ctx, op.target)
        if op.action == "update_price":
            if "price" not in op.fields:
                raise ToolError("VALIDATION_FAILED", "no price given", guidance="Ask the owner for the new price.")
            price = _paise(op.fields["price"])
            if offering.variants:
                variant = offering.pick_variant(op.target)
                if variant is None:
                    sizes = ", ".join(v.name for v in offering.variants)
                    raise ToolError(
                        "AMBIGUOUS_MATCH", f"{offering.name} has several sizes", guidance=f"Ask which size: {sizes}."
                    )
                await knowledge.add(cs, "variant", "update", variant.variant_id, {"price_paise": price})
            else:
                await knowledge.add(cs, "offering", "update", offering.offering_id, {"base_price_paise": price})
        elif op.action == "update":
            allowed = {k: v for k, v in op.fields.items() if k in ("description", "category", "name")}
            if not allowed:
                raise ToolError("VALIDATION_FAILED", "nothing to update", guidance="Ask what should change.")
            await knowledge.add(cs, "offering", "update", offering.offering_id, allowed)
        else:
            status = "hidden" if op.action == "hide" else "active"
            await knowledge.add(cs, "offering", "update", offering.offering_id, {"status": status})
    preview = await knowledge.preview(ctx.business_id, cs.id)
    if not preview.lines:
        return {"read_back": [], "next": "Nothing would change (already like that). Tell the owner."}
    return await _confirmation(ctx, {"change_set_id": str(cs.id)}, "Catalogue change", preview.lines)


# ---------------------------------------------------------------- knowledge / rules


class KnowledgeChangeArgs(BaseModel):
    kind: Literal["qa", "cod_rule"]
    change: dict[str, Any] = Field(
        description="qa: {question, answer} (a fact customers ask about); "
        "cod_rule: {enabled: bool, max_total: rupees or null}"
    )


@tool("knowledge_change", KnowledgeChangeArgs, desks=OWNER, side_effect=True)
async def knowledge_change(ctx: ToolContext, args: KnowledgeChangeArgs, s: AsyncSession) -> dict[str, Any]:
    """Save a fact for customers (Q&A) or set the cash-on-delivery rule. Read-back + confirmation."""
    if args.kind == "qa":
        question, answer = str(args.change.get("question") or ""), str(args.change.get("answer") or "")
        if not question or not answer:
            raise ToolError("VALIDATION_FAILED", "question and answer needed", guidance="Ask for the missing part.")
        cs = await knowledge.begin(ctx.business_id, "owner_command", "Q&A", member_id=ctx.member_id)
        await knowledge.add(cs, "qa", "create", None, {"question": question, "answer": answer, "scope": "global"})
        preview = await knowledge.preview(ctx.business_id, cs.id)
        return await _confirmation(ctx, {"change_set_id": str(cs.id)}, "Q&A", preview.lines)
    enabled = bool(args.change.get("enabled", True))
    cap = args.change.get("max_total")
    rule = {"enabled": enabled, "max_total_paise": _paise(cap) if enabled and cap else None}
    cap_paise = rule["max_total_paise"]
    if not enabled:
        line = "💵 Cash on delivery: off"
    elif cap_paise:
        line = f"💵 Cash on delivery: on, for orders up to {format_inr(int(cap_paise))}"
    else:
        line = "💵 Cash on delivery: on, for all orders"
    return await _confirmation(ctx, {"settings_patch": {"cod_rule": rule}}, "COD rule", [line])


# ---------------------------------------------------------------- numbers and pending work


class StatsArgs(BaseModel):
    period: Literal["today", "yesterday", "week"] = "today"


async def _period(ctx: ToolContext, period: str) -> tuple[datetime, datetime]:
    settings = await knowledge.business_settings(ctx.business_id)
    local = to_business_tz(utcnow(), str(settings.get("timezone") or "Asia/Kolkata"))
    start_day = local.date() - timedelta(days={"today": 0, "yesterday": 1, "week": 6}[period])
    start = datetime.combine(start_day, time(0), tzinfo=local.tzinfo)
    end = datetime.combine(
        local.date() + timedelta(days=0 if period == "yesterday" else 1), time(0), tzinfo=local.tzinfo
    )
    return start, end


@tool("business_stats", StatsArgs, desks=OWNER)
async def business_stats(ctx: ToolContext, args: StatsArgs) -> dict[str, Any]:
    """Orders, revenue, conversations, pending questions and unpaid links for a period. Report only these numbers."""
    start, end = await _period(ctx, args.period)
    async with get_db().tenant_session(ctx.business_id) as s:
        r = (
            await s.execute(
                text("""SELECT
                    (SELECT count(*) FROM orders WHERE confirmed_at >= :a AND confirmed_at < :b) AS confirmed,
                    (SELECT COALESCE(sum(total_paise), 0) FROM orders WHERE confirmed_at >= :a AND confirmed_at < :b
                        AND payment_status = 'paid') AS paid_paise,
                    (SELECT count(*) FROM orders WHERE confirmed_at >= :a AND confirmed_at < :b
                        AND payment_status = 'cod_pending') AS cod,
                    (SELECT count(*) FROM orders WHERE status = 'awaiting_payment') AS awaiting_payment,
                    (SELECT count(*) FROM orders WHERE status = 'draft' AND created_at >= :a) AS drafts,
                    (SELECT count(DISTINCT conversation_id) FROM messages WHERE direction = 'inbound'
                        AND created_at >= :a AND created_at < :b) AS conversations,
                    (SELECT count(*) FROM owner_questions WHERE status IN ('open', 'asked')) AS open_questions"""),
                {"a": start, "b": end},
            )
        ).one()
    return {
        "period": args.period,
        "orders_confirmed": r.confirmed,
        "revenue_paid": format_inr(int(r.paid_paise)),
        "cod_orders": r.cod,
        "awaiting_payment_now": r.awaiting_payment,
        "draft_orders": r.drafts,
        "customer_conversations": r.conversations,
        "open_questions": r.open_questions,
    }


class NoArgs(BaseModel):
    pass


@tool("pending_items", NoArgs, desks=OWNER)
async def pending_items(ctx: ToolContext, args: NoArgs) -> dict[str, Any]:
    """Open customer questions, changes waiting for confirmation, and orders unpaid for over 24 hours."""
    async with get_db().tenant_session(ctx.business_id) as s:
        questions = (
            await s.execute(
                text("""SELECT q.short_code, q.question, q.asked_at, c.display_name FROM owner_questions q
                        JOIN customers c ON c.id = q.customer_id WHERE q.status IN ('open', 'asked')
                        ORDER BY q.asked_at DESC LIMIT 10""")
            )
        ).all()
        unpaid = (
            await s.execute(
                text("""SELECT order_number, total_paise, created_at FROM orders WHERE status = 'awaiting_payment'
                        AND created_at < now() - interval '24 hours' ORDER BY created_at LIMIT 10""")
            )
        ).all()
    waiting = await approvals.pending(ctx.business_id)
    return {
        "questions": [
            {"code": q.short_code.strip(), "customer": q.display_name or "customer", "question": q.question}
            for q in questions
        ],
        "awaiting_confirmation": [{"code": a.short_code, "what": a.preview.get("summary")} for a in waiting],
        "unpaid_over_24h": [{"order": o.order_number, "total": format_inr(o.total_paise)} for o in unpaid],
    }


class AnswerPendingArgs(BaseModel):
    item_ref: str | None = Field(default=None, description="Question code (e.g. 1234); omit for the latest open one")
    answer: str
    scope: Literal["global", "customer", "temporary"] | None = None


@tool("answer_pending", AnswerPendingArgs, desks=OWNER, side_effect=True)
async def answer_pending(ctx: ToolContext, args: AnswerPendingArgs, s: AsyncSession) -> dict[str, Any]:
    """Answer an open customer question in the owner's words; it is relayed to the customer and remembered."""
    row = (
        await s.execute(
            text("""SELECT q.id, q.short_code, c.display_name FROM owner_questions q
                    JOIN customers c ON c.id = q.customer_id
                    WHERE q.status IN ('open', 'asked') AND (CAST(:code AS text) IS NULL OR q.short_code = :code)
                    ORDER BY q.asked_at DESC LIMIT 2"""),
            {"code": (args.item_ref or "").strip() or None},
        )
    ).all()
    if not row:
        raise ToolError("NOT_FOUND", "no open question", guidance="Tell the owner there is nothing pending.")
    if len(row) > 1 and not args.item_ref:
        codes = ", ".join(f"{r.short_code.strip()} ({r.display_name or 'customer'})" for r in row)
        raise ToolError("AMBIGUOUS_MATCH", "several open questions", guidance=f"Ask which one: {codes}.")
    port = ports.registry.require(QuestionAnswerPort)  # type: ignore[type-abstract]
    result = await port.answer(ctx.business_id, row[0].id, args.answer, member_id=ctx.member_id, scope=args.scope)
    return {
        "question": result.question_code,
        "customer": row[0].display_name or "customer",
        "saved_for": result.scope,
        "relayed": result.relayed,
        "scope_uncertain": result.scope_uncertain,
    }


class UndoArgs(BaseModel):
    change_set_ref: str | None = Field(default=None, description="Omit to undo the last change")


@tool("undo", UndoArgs, desks=OWNER, side_effect=True)
async def undo(ctx: ToolContext, args: UndoArgs, s: AsyncSession) -> dict[str, Any]:
    """Undo the owner's last applied change (catalogue or Q&A). Report what was undone."""
    try:
        done = await knowledge.revert(
            ctx.business_id, UUID(args.change_set_ref) if args.change_set_ref else None, member_id=ctx.member_id
        )
    except LookupError as exc:
        raise ToolError("NOT_FOUND", "nothing to undo", guidance="Tell the owner there is nothing to undo.") from exc
    except knowledge.ChangeConflict as exc:
        raise ToolError(
            "CONFLICT", "those items changed after the edit", guidance="Ask what they want the values to be now."
        ) from exc
    preview = await knowledge.preview(ctx.business_id, done.id)
    return {"undone": done.summary, "lines": preview.lines}


# ---------------------------------------------------------------- files and customers


class FilesSearchArgs(BaseModel):
    query: str | None = None
    kind: Literal["image", "pdf", "spreadsheet", "document", "audio", "other"] | None = None
    since: Literal["today", "yesterday", "week"] | None = None


@tool("files_search", FilesSearchArgs, desks=OWNER)
async def files_search(ctx: ToolContext, args: FilesSearchArgs) -> dict[str, Any]:
    """The owner's file library (files sent on any channel), newest first. Reference files as file:<id>."""
    since = (await _period(ctx, args.since))[0] if args.since else None
    files = await owner.search_files(ctx.business_id, query=args.query, kind=args.kind, since=since)
    return {
        "files": [
            {
                "file": f"file:{f.id}",
                "name": f.filename,
                "kind": f.kind,
                "description": f.description or "(not described yet)",
                "via": f.received_via,
                "received": f.created_at.isoformat(),
            }
            for f in files
        ]
    }


class SendToCustomerArgs(BaseModel):
    customer_ref: str = Field(description="Customer name, phone or order number (e.g. MB-1001)")
    message: str
    send_now: bool = Field(default=False, description="True only if the owner explicitly said to send right away")


@tool("send_to_customer", SendToCustomerArgs, desks=OWNER, side_effect=True)
async def send_to_customer(ctx: ToolContext, args: SendToCustomerArgs, s: AsyncSession) -> dict[str, Any]:
    """Send a message to a customer on their most recent channel. Read-back first unless the owner said 'send now'."""
    ref = args.customer_ref.strip()
    rows = (
        await s.execute(
            text("""SELECT DISTINCT ON (cu.id) cu.id, cu.display_name, cu.phone_e164, cv.id AS conversation_id,
                           cv.channel, cv.last_inbound_at
                    FROM customers cu JOIN conversations cv ON cv.customer_id = cu.id
                    LEFT JOIN orders o ON o.customer_id = cu.id
                    WHERE cu.display_name ILIKE '%' || :r || '%'
                       OR (length(:digits) >= 6 AND cu.phone_e164 LIKE '%' || :digits)
                       OR upper(o.order_number) = upper(:r)
                    ORDER BY cu.id, cv.last_inbound_at DESC NULLS LAST LIMIT 5"""),
            {"r": ref, "digits": "".join(ch for ch in ref if ch.isdigit())},
        )
    ).all()
    if not rows:
        raise ToolError(
            "NOT_FOUND", f"no customer matches '{ref}'", guidance="Ask for the name, phone or order number."
        )
    if len(rows) > 1:
        names = ", ".join(f"{r.display_name or r.phone_e164}" for r in rows)
        raise ToolError("AMBIGUOUS_MATCH", "several customers match", guidance=f"Ask which one: {names}.")
    c = rows[0]
    who, channel = c.display_name or c.phone_e164 or "customer", str(c.channel).replace("_", " ")
    if args.send_now:
        await gateway.outbound().enqueue(
            s,
            business_id=ctx.business_id,
            conversation_id=c.conversation_id,
            blocks=[{"type": "text", "text": args.message}],
            origin="owner_command",
            idempotency_base=f"owner_send:{ctx.idem()}",
        )
        return {"sent": True, "to": who, "channel": channel}
    return await _confirmation(
        ctx,
        {"conversation_id": str(c.conversation_id), "text": args.message, "to": who},
        f"Message to {who}",
        [f"📤 To {who} ({channel}): “{args.message}”"],
        kind="outbound_draft",
    )


class OrdersListArgs(BaseModel):
    filter: Literal["today", "upcoming", "unpaid", "recent"] = "today"


@tool("orders_list", OrdersListArgs, desks=OWNER)
async def orders_list(ctx: ToolContext, args: OrdersListArgs) -> dict[str, Any]:
    """Orders: confirmed today, upcoming deliveries, awaiting payment, or the most recent."""
    start, end = await _period(ctx, "today")
    async with get_db().tenant_session(ctx.business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT o.order_number, o.status, o.payment_status, o.total_paise, o.delivery_date,
                               o.delivery_slot, c.display_name FROM orders o JOIN customers c ON c.id = o.customer_id
                        WHERE CASE CAST(:f AS text)
                                WHEN 'today' THEN o.confirmed_at >= :a AND o.confirmed_at < :b
                                WHEN 'upcoming' THEN o.status IN ('confirmed', 'preparing')
                                                     AND o.delivery_date >= CAST(:a AS date)
                                WHEN 'unpaid' THEN o.status = 'awaiting_payment'
                                ELSE o.status <> 'draft' END
                        ORDER BY COALESCE(o.delivery_date, o.created_at::date), o.created_at LIMIT 15"""),
                {"f": args.filter, "a": start, "b": end},
            )
        ).all()
    return {
        "orders": [
            {
                "order": r.order_number,
                "customer": r.display_name or "customer",
                "status": str(r.status),
                "payment": str(r.payment_status),
                "total": format_inr(r.total_paise),
                "delivery": f"{r.delivery_date:%a %d %b} {r.delivery_slot or ''}".strip() if r.delivery_date else None,
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------- imports and confirmations


class ImportFromFileArgs(BaseModel):
    file_ref: str = Field(description="file:<id> from RECENT FILES or files_search")
    mode: Literal["catalog_update", "onboarding"] = "catalog_update"


@tool("import_from_file", ImportFromFileArgs, desks=OWNER, side_effect=True)
async def import_from_file(ctx: ToolContext, args: ImportFromFileArgs, s: AsyncSession) -> dict[str, Any]:
    """Read a menu / price list (photo, PDF, Excel, CSV) and prepare the catalogue changes for the owner to confirm.
    Runs in the background; the summary with a confirmation code follows in a moment."""
    try:
        file_id = UUID(args.file_ref.removeprefix("file:").strip())
    except ValueError as exc:
        raise ToolError("INVALID_ARGS", "bad file ref", guidance="Use file:<id> from RECENT FILES.") from exc
    f = await owner.get_file(ctx.business_id, file_id)
    if f is None:
        raise ToolError("NOT_FOUND", "no such file", guidance="Use files_search to find the file.")
    if f.kind not in ("image", "pdf", "spreadsheet"):
        raise ToolError("VALIDATION_FAILED", f"can't import a {f.kind}", guidance="Ask for a photo, PDF or Excel.")
    assert ctx.conversation_id is not None
    run_id = await ports.registry.require(WorkflowPort).start_import(  # type: ignore[type-abstract]
        ctx.business_id, f.id, mode=args.mode, member_id=ctx.member_id, reply_conversation_id=ctx.conversation_id
    )
    return {
        "started": True,
        "import_run": str(run_id),
        "next": "Tell the owner in one short line that you're reading the file and the summary will follow.",
    }


class ConfirmPendingArgs(BaseModel):
    code: str = Field(description="The 4-digit code of the change waiting for confirmation (see PENDING)")
    approve: bool


@tool("confirm_pending", ConfirmPendingArgs, desks=OWNER, side_effect=True)
async def confirm_pending(ctx: ToolContext, args: ConfirmPendingArgs, s: AsyncSession) -> dict[str, Any]:
    """Confirm or cancel a change waiting for confirmation when the owner agrees in their own words
    ("theek hai lagao", "haan apply karo, custom cake baad mein")."""
    outcome = await owner.execute_decision(
        ports.registry, ctx.business_id, ctx.member_id, args.approve, code=args.code.strip()
    )
    if outcome is None:
        raise ToolError("NOT_FOUND", f"nothing waiting with code {args.code}", guidance="Check PENDING for the code.")
    return {"result": outcome or "Done — the result follows in a moment."}


# ---------------------------------------------------------------- terminal


class OwnerReply(BaseModel):
    text: str = Field(min_length=1, max_length=3500, description="Your message to the owner (brief)")


@tool("reply_owner", OwnerReply, desks=OWNER, terminal=True)
async def reply_owner(ctx: ToolContext, args: OwnerReply) -> dict[str, Any]:
    """Finish the turn with your message to the owner. Call exactly once, last."""
    return {"ok": True}


OWNER_TOOLS = [
    "catalog_search",
    "knowledge_lookup",
    "catalog_change",
    "knowledge_change",
    "business_stats",
    "pending_items",
    "answer_pending",
    "undo",
    "files_search",
    "send_to_customer",
    "orders_list",
    "import_from_file",
    "confirm_pending",
    "reply_owner",
]
