"""Desk output and gate schemas (POC_04 §5, §6)."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

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
# ...unless a capability decided it this turn (ADR-015): an action tool ran under the owner's playbook, or the
# owner's decision is being relayed. Then the reply reports a real outcome and is not overridden. A reply that only
# states the shop's own policy (cites profile / Q&A, no order) is information and is not overridden either.
CAPABILITY_TOPICS = frozenset({"returns_refunds_cancellation"})


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


class AgentOutput(BaseModel):
    """Arguments of the terminal `reply` tool."""

    action: Literal["send", "ask_owner", "handoff", "no_reply"]
    blocks: list[ModelBlock] = Field(default_factory=list)
    referenced_records: list[RecordRef] = Field(default_factory=list)
    topic: Topic
    language: Language
    owner_question: OwnerQuestionDraft | None = None
    owner_brief: str | None = Field(
        default=None,
        max_length=600,
        description="Only for ask_owner / handoff: 2–3 short lines for the shop owner, written as a colleague "
        "would: who, what they need, the relevant facts, what you suggest. No transcript.",
    )
    customer_facts: list[MemoryFact] = Field(default_factory=list)
    flags: OutputFlags = Field(default_factory=OutputFlags)


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
    capability_decided: bool = False  # an action tool ran this turn, or the owner's decision is being relayed
    # one of our tools answered this turn: the reply may rest on that answer even when it has no record to cite
    # (eval: "mera order kahan hai?" → order_status found none → "no order yet" fell back for lack of a ref)
    tool_backed: bool = False
    # facts this reply must carry (the privacy notice in a customer's first reply: shop_name, privacy_link)
    required_facts: dict[str, str] = Field(default_factory=dict)
