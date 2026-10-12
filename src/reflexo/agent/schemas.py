"""Desk output and gate schemas (POC_04 §5, §6)."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from reflexo.common.blocks import (
    ButtonsBlock,
    CarouselBlock,
    ListBlock,
    OrderSummaryBlock,
    PaymentLinkBlock,
    ProductBlock,
    TextBlock,
)

# Blocks the model may produce (images/files are sent by tools, never typed by the model).
ModelBlock = Annotated[
    TextBlock | ButtonsBlock | ListBlock | ProductBlock | CarouselBlock | PaymentLinkBlock | OrderSummaryBlock,
    Field(discriminator="type"),
]

Topic = Literal[
    "greeting_smalltalk", "hours_location_contact", "catalog_info", "pricing", "availability_stock",
    "delivery_shipping", "payments", "order_status", "booking_appointments", "returns_refunds_cancellation",
    "discount_negotiation", "complaint", "custom_request", "human_request", "spam_abuse", "other",
]  # fmt: skip
Language = Literal["en", "hi", "hi-Latn"]
RecordType = Literal["offering", "variant", "qa", "order", "payment_link", "delivery_zone", "profile"]

GROUNDED_TOPICS = frozenset(
    {"catalog_info", "pricing", "availability_stock", "delivery_shipping", "payments", "order_status",
     "hours_location_contact"}
)  # fmt: skip
HANDOFF_TOPICS = frozenset({"returns_refunds_cancellation", "complaint", "human_request"})


class RecordRef(BaseModel):
    type: RecordType
    id: str = Field(description="UUID of the record, or the zone id / profile section name")


class OwnerQuestionDraft(BaseModel):
    question: str = Field(description="The customer's question, short and clear, for the owner")
    ai_guess: str | None = Field(default=None, description="Your best guess at the answer, or null")
    options: list[str] = Field(default_factory=list, max_length=3, description="Up to 3 quick answers")


class MemoryFact(BaseModel):
    key: str = Field(description="e.g. dietary, favourite_flavour, address_area, name")
    value: str


class OutputFlags(BaseModel):
    angry: bool = False
    possible_spam: bool = False


CHOICE_TEXT_MAX = 1024  # body limit of ButtonsBlock / ListBlock (WhatsApp interactive body)


class AgentOutput(BaseModel):
    """Arguments of the terminal `reply` tool."""

    action: Literal["send", "ask_owner", "handoff", "no_reply"]
    blocks: list[ModelBlock] = Field(default_factory=list)
    referenced_records: list[RecordRef] = Field(default_factory=list)
    topic: Topic
    language: Language
    owner_question: OwnerQuestionDraft | None = None
    customer_facts: list[MemoryFact] = Field(default_factory=list)
    flags: OutputFlags = Field(default_factory=OutputFlags)

    @model_validator(mode="before")
    @classmethod
    def _choices_take_the_text_before_them(cls, data: Any) -> Any:
        """A buttons/list block without its own text, right after a text block, uses that text as its body —
        the intent is unambiguous, so normalise instead of rejecting (live: the reply and its repair both left
        the buttons text empty and the customer got a fallback)."""
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            return data
        blocks: list[Any] = []
        for block in data["blocks"]:
            prev = blocks[-1] if blocks else None
            if (
                isinstance(block, dict)
                and block.get("type") in ("buttons", "list")
                and not block.get("text")
                and isinstance(prev, dict)
                and prev.get("type", "text") == "text"
                and 0 < len(str(prev.get("text") or "")) <= CHOICE_TEXT_MAX
            ):
                blocks[-1] = {**block, "text": prev["text"]}
            else:
                blocks.append(block)
        return {**data, "blocks": blocks}


class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str | None = None
    guidance: str | None = None  # instruction for the repair round


class GateResult(BaseModel):
    passed: bool
    checks: list[GateCheck]
    rewritten_action: str | None = None  # tier rules may turn `send` into handoff/ask_owner
    duration_ms: float = 0.0

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    def repair_message(self) -> str:
        lines = ["Your reply was NOT sent. Fix these problems and call reply again:"]
        for c in self.failures:
            lines.append(f"- {c.name}: {c.detail or ''}" + (f" → {c.guidance}" if c.guidance else ""))
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": [c.name for c in self.failures],
            "rewritten_action": self.rewritten_action,
            "ms": round(self.duration_ms, 2),
        }


class GroundingRecords(BaseModel):
    """Hydrated facts the gate checks text against (re-read under RLS at gate time)."""

    business_id: UUID
    valid_refs: set[tuple[str, str]] = Field(default_factory=set)
    missing_refs: list[tuple[str, str]] = Field(default_factory=list)
    amounts_paise: set[int] = Field(default_factory=set)  # every money amount the reply may state
    quantity_numbers: set[int] = Field(default_factory=set)  # non-money numbers in names ("Box of 6", "500 g")
    discount_texts: list[str] = Field(default_factory=list)  # Q&A answers that mention a discount
    price_on_request: bool = False
    allowed_hosts: set[str] = Field(default_factory=set)
    allowed_urls: set[str] = Field(default_factory=set)
    allowed_phones: set[str] = Field(default_factory=set)  # digits only
    allowed_emails: set[str] = Field(default_factory=set)
    block_offering_ids: set[str] = Field(default_factory=set)  # offerings that product/carousel blocks may show
    known_numbers: set[str] = Field(default_factory=set)  # plain numbers the system showed the model this turn
