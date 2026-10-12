"""Knowledge models (POC_05 §1): business profile, offering attributes, search results, change sets."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reflexo.common.blocks import ProductView, VariantLine
from reflexo.common.money import format_inr

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")  # leaf provenance (`_src`, `_at`) and owner-added keys survive


# ---------------------------------------------------------------- business profile


class Interval(BaseModel):
    open: str
    close: str

    @field_validator("open", "close")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("time must be HH:MM (24 h)")
        return v


class DayHours(BaseModel):
    day: Day
    intervals: list[Interval] = []


class DateException(BaseModel):
    date: date
    closed: bool = True
    intervals: list[Interval] = []
    note: str | None = None


class Hours(BaseModel):
    weekly: list[DayHours] = []
    exceptions: list[DateException] = []


class DeliveryZone(BaseModel):
    id: str
    label: str
    pincodes: list[str] = []
    areas: list[str] = []
    fee_paise: int = Field(ge=0)
    free_above_paise: int | None = None
    eta: str = ""


class Delivery(_Model):
    notice_hours: int = Field(default=24, ge=0)
    pickup_available: bool = False
    pickup_address: str | None = None
    zones: list[DeliveryZone] = []


class Identity(_Model):
    name: str
    description: str | None = None
    category: str | None = None
    owner_name: str | None = None


class Contact(_Model):
    phone: str | None = None
    address: str | None = None
    maps_url: str | None = None
    website: str | None = None


class Policies(_Model):
    returns: str | None = None
    cancellation: str | None = None
    custom_orders: str | None = None


class Payments(_Model):
    methods: list[str] = []


class Persona(_Model):
    name: str | None = None
    tone: str = "friendly"
    formality: str = "aap"
    emoji: str = "light"
    greeting: str | None = None
    languages: list[str] = ["hi-Latn", "en"]


class BusinessProfile(_Model):
    identity: Identity
    contact: Contact = Contact()
    hours: Hours = Hours()
    delivery: Delivery = Delivery()
    policies: Policies = Policies()
    payments: Payments = Payments()
    persona: Persona = Persona()


class OfferingAttributes(_Model):
    eggless_available: bool | None = None
    eggless_extra_paise: int | None = None
    flavours: list[str] = []
    weights: list[str] = []
    serves: str | None = None
    notice_hours: int | None = None


# ---------------------------------------------------------------- results


class VariantInfo(BaseModel):
    variant_id: UUID | None
    name: str
    price_paise: int | None
    in_stock: bool = True

    @property
    def price_display(self) -> str:
        return format_inr(self.price_paise) if self.price_paise is not None else "price on request"


class CatalogCard(BaseModel):
    """A search hit hydrated from live rows: prices/stock never come from the index (D-041)."""

    offering_id: UUID
    name: str
    description: str | None
    category: str | None
    attributes: dict[str, Any] = {}
    variants: list[VariantInfo]
    price_on_request: bool
    in_stock: bool
    image_media_id: UUID | None = None
    matched_variant_id: UUID | None = None
    score: float = 0.0

    def product_view(self, image_url: str | None = None) -> ProductView:
        return ProductView(
            offering_id=self.offering_id,
            name=self.name,
            description=self.description,
            lines=[
                VariantLine(variant_id=v.variant_id, name=v.name, price_display=v.price_display) for v in self.variants
            ],
            price_on_request=self.price_on_request,
            image_url=image_url,
        )


class KnowledgeHit(BaseModel):
    entity_type: Literal["qa", "profile"]
    entity_id: UUID
    title: str
    text: str
    scope: str = "global"
    score: float = 0.0


class SpeculativeResult(BaseModel):
    catalog: list[CatalogCard] = []
    knowledge: list[KnowledgeHit] = []
    timed_out: bool = False
    elapsed_ms: int = 0


class DeliveryQuote(BaseModel):
    serviceable: bool
    zone_id: str | None = None
    zone_label: str | None = None
    fee_paise: int | None = None
    free_above_paise: int | None = None
    eta: str | None = None
    notice_hours: int
    pickup_available: bool
    pickup_address: str | None = None
    matched_by: Literal["pincode", "area", "none"] = "none"


class RenderedProfile(BaseModel):
    business_id: UUID
    version: int
    text: str


# ---------------------------------------------------------------- change sets

EntityType = Literal["offering", "variant", "qa", "profile", "detail_schema", "playbook_rule"]
ChangeOp = Literal["create", "update", "delete"]


class ChangeItem(BaseModel):
    id: UUID
    entity_type: EntityType
    entity_id: UUID
    op: ChangeOp
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class ChangeSet(BaseModel):
    id: UUID
    business_id: UUID
    origin: str
    summary: str
    status: str
    items: list[ChangeItem] = []
    revert_of: UUID | None = None


class ChangeSetPreview(BaseModel):
    change_set_id: UUID
    summary: str
    lines: list[str]


class ChangeConflict(Exception):
    """A row changed after the draft was built; the set was not applied (POC_05 §1.4 conflict rule)."""

    def __init__(self, change_set_id: UUID, entity_type: str, entity_id: UUID, fields: list[str]) -> None:
        super().__init__(f"{entity_type} {entity_id} changed since the draft: {', '.join(fields)}")
        self.change_set_id = change_set_id
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.fields = fields
