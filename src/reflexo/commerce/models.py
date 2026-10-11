"""Commerce models (POC_05 §4.2)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from reflexo.common.money import format_inr

PaymentMethod = Literal["razorpay_link", "cod"]


class CommerceError(Exception):
    """A business-rule failure the agent can act on: `guidance` tells the model what to do next."""

    def __init__(
        self, code: str, message: str, *, guidance: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.guidance = guidance
        self.details = details or {}


class ItemRef(BaseModel):
    offering_id: UUID
    variant_id: UUID | None = None
    quantity: int = Field(default=1, ge=1, le=50)
    details: dict[str, Any] = Field(default_factory=dict)


class OrderLine(BaseModel):
    line_id: UUID
    name: str
    quantity: int
    unit_price_paise: int
    line_total_paise: int
    offering_id: UUID | None
    variant_id: UUID | None
    adjustment: str | None = None  # server-added lines, e.g. "eggless"

    @property
    def price_display(self) -> str:
        return format_inr(self.line_total_paise)


class DetailField(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    options: list[str] = []
    hint: str = ""
    value: Any = None


class OrderView(BaseModel):
    order_id: UUID
    order_number: str
    status: str
    payment_method: str | None
    payment_status: str
    lines: list[OrderLine]
    subtotal_paise: int
    delivery_fee_paise: int
    total_paise: int
    fields: list[DetailField]
    missing_details: list[str]
    delivery_date: str | None = None
    delivery_slot: str | None = None
    address: str | None = None
    customer_id: UUID
    conversation_id: UUID | None = None
    # details from the last update that were not accepted (the valid ones in the same call are kept)
    detail_errors: list[dict[str, Any]] = Field(default_factory=list)

    def tool_view(self) -> dict[str, Any]:
        """Compact view for the model: displays only, ids for references."""
        return {
            "order_id": str(self.order_id),
            "order_number": self.order_number,
            "status": self.status,
            "payment_status": self.payment_status,
            "lines": [
                {"line_id": str(ln.line_id), "item": ln.name, "qty": ln.quantity, "price": ln.price_display}
                for ln in self.lines
            ],
            "subtotal": format_inr(self.subtotal_paise),
            "delivery_fee": format_inr(self.delivery_fee_paise),
            "total": format_inr(self.total_paise),
            "details": {f.key: f.value for f in self.fields if f.value not in (None, "")},
            "missing_details": [
                {"key": f.key, "label": f.label, "options": f.options, "hint": f.hint}
                for f in self.fields
                if f.key in self.missing_details
            ],
            **({"not_accepted": self.detail_errors} if self.detail_errors else {}),
        }


class ConfirmResult(BaseModel):
    status: Literal["awaiting_payment", "confirmed", "declined"]
    order: OrderView
    payment_link_id: UUID | None = None
    payment_url: str | None = None
    decline_code: str | None = None
    decline_reason: str | None = None
    alternatives: list[str] = []
