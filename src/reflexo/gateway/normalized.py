"""Provider-neutral inbound events produced by adapter `parse()` (POC_03 §3.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from reflexo.common.enums import ChannelProvider, ChannelType

EventKind = Literal["message", "interactive_reply", "status", "echo", "call", "comment", "referral"]


class InboundMedia(BaseModel):
    provider_media_id: str
    kind: Literal["image", "document", "audio", "video", "sticker"]
    mime: str | None = None
    filename: str | None = None
    caption: str | None = None
    url: str | None = None  # simulator / platforms that send direct URLs


class InteractiveReply(BaseModel):
    option_id: str
    title: str | None = None


class GeoPoint(BaseModel):
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


class DeliveryStatus(BaseModel):
    provider_message_id: str
    status: Literal["sent", "delivered", "read", "failed"]
    error_code: str | None = None
    error_message: str | None = None


class CallSignal(BaseModel):
    call_id: str
    event: str  # connect | terminate | status values from Meta
    sdp: str | None = None
    sdp_type: str | None = None
    caller_wa_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CommentInfo(BaseModel):
    comment_id: str
    post_id: str | None = None
    parent_id: str | None = None


class NormalizedEvent(BaseModel):
    kind: EventKind
    provider: ChannelProvider
    channel: ChannelType
    route_key: str
    occurred_at: datetime
    thread_key: str = ""
    external_message_id: str | None = None
    sender_external_id: str | None = None
    sender_display_name: str | None = None
    text: str | None = None
    media: list[InboundMedia] = Field(default_factory=list)
    interactive: InteractiveReply | None = None
    location: GeoPoint | None = None
    status: DeliveryStatus | None = None
    call: CallSignal | None = None
    comment: CommentInfo | None = None
    referral_code: str | None = None  # e.g. Telegram /start payload or ad referral ref
    echo_app_id: str | None = None  # app that sent an echoed message (None = sent by a human in the inbox)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def ends_thought(self) -> bool:
        """Short coalescing window when the message clearly completes a thought (POC_03 §4.1)."""
        if self.interactive is not None or self.media:
            return True
        body = (self.text or "").strip()
        return body.endswith("?") or len(body.split()) >= 12
