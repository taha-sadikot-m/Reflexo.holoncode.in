"""Content blocks: the channel-neutral output vocabulary of agents and automations (POC_03 §8.1, D-063).

`product`, `payment_link` and `order_summary` carry only ids; their content (names, prices, URLs, totals) is
hydrated from the database at render time, so the model can never supply a price or link.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class Option(BaseModel):
    id: str = Field(min_length=1, max_length=48)
    title: str = Field(min_length=1, max_length=80)
    aliases: list[str] = []


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=8000)


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    media_object_id: UUID | None = None
    url: str | None = None
    caption: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _source(self) -> ImageBlock:
        if (self.media_object_id is None) == (self.url is None):
            raise ValueError("image needs exactly one of media_object_id or url")
        return self


class FileBlock(BaseModel):
    type: Literal["file"] = "file"
    media_object_id: UUID
    filename: str
    caption: str | None = Field(default=None, max_length=1024)


class ButtonsBlock(BaseModel):
    type: Literal["buttons"] = "buttons"
    text: str = Field(min_length=1, max_length=1024)
    options: list[Option] = Field(min_length=1, max_length=10)


class ListBlock(BaseModel):
    type: Literal["list"] = "list"
    text: str = Field(min_length=1, max_length=1024)
    button_label: str = Field(default="Options", max_length=20)
    options: list[Option] = Field(min_length=1, max_length=30)


class ProductBlock(BaseModel):
    type: Literal["product"] = "product"
    offering_id: UUID
    variant_id: UUID | None = None
    cta: list[Option] = Field(default_factory=list, max_length=3)


class CarouselBlock(BaseModel):
    type: Literal["carousel"] = "carousel"
    items: list[ProductBlock] = Field(min_length=1, max_length=10)


class PaymentLinkBlock(BaseModel):
    type: Literal["payment_link"] = "payment_link"
    payment_link_id: UUID


class OrderSummaryBlock(BaseModel):
    type: Literal["order_summary"] = "order_summary"
    order_id: UUID


ContentBlock = Annotated[
    TextBlock
    | ImageBlock
    | FileBlock
    | ButtonsBlock
    | ListBlock
    | ProductBlock
    | CarouselBlock
    | PaymentLinkBlock
    | OrderSummaryBlock,
    Field(discriminator="type"),
]


class BlockList(BaseModel):
    blocks: list[ContentBlock] = Field(min_length=1)


# ---------------------------------------------------------------- hydrated views (filled from DB rows)


class VariantLine(BaseModel):
    variant_id: UUID | None
    name: str
    price_display: str


class ProductView(BaseModel):
    offering_id: UUID
    name: str
    description: str | None = None
    lines: list[VariantLine]
    price_on_request: bool = False
    image_url: str | None = None


class PaymentLinkView(BaseModel):
    payment_link_id: UUID
    url: str
    amount_display: str
    description: str


class OrderSummaryView(BaseModel):
    order_id: UUID
    order_number: str
    lines: list[str]
    total_display: str
    delivery: str | None = None
    payment: str | None = None
