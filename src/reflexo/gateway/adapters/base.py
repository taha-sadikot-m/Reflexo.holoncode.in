"""Channel adapter contract and the intermediate render representation (POC_03 §8, §10.1).

Rendering is two-stage:
1. `gateway.render` turns content blocks into channel-neutral IR parts, applying the capability manifest and
   deterministic degradation (carousel → list → numbered text).
2. The adapter turns IR parts into provider API request bodies (`to_payloads`) and sends them (`send`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from reflexo.common.blocks import Option, ProductView
from reflexo.common.contracts import SendResult
from reflexo.common.enums import ChannelProvider
from reflexo.gateway.normalized import InboundMedia, NormalizedEvent

# ---------------------------------------------------------------- capability manifest


@dataclass(frozen=True)
class Manifest:
    provider: str
    max_text: int
    max_buttons: int
    button_title_max: int
    list_supported: bool
    list_max_rows: int
    row_title_max: int
    carousel_supported: bool
    carousel_max: int
    caption_max: int
    link_button: bool
    session_window_hours: int | None  # None = no customer-service window
    quick_replies_max: int = 0  # Messenger/IG quick replies attached to a text


# ---------------------------------------------------------------- IR parts


@dataclass(frozen=True)
class IRText:
    text: str


@dataclass(frozen=True)
class IRImage:
    url: str
    caption: str | None = None


@dataclass(frozen=True)
class IRFile:
    url: str
    filename: str
    caption: str | None = None


@dataclass(frozen=True)
class IRChoices:
    text: str
    options: list[Option]
    style: str  # "buttons" | "list" | "quick_replies"
    button_label: str = "Options"


@dataclass(frozen=True)
class IRCards:
    items: list[ProductView]
    cta_label: str = "Order"


@dataclass(frozen=True)
class IRLink:
    text: str
    url: str
    label: str


IRPart = IRText | IRImage | IRFile | IRChoices | IRCards | IRLink


@dataclass
class RenderContext:
    recipient: str  # provider recipient id (wa_id digits, PSID, IGSID, Telegram chat id, simulator thread)
    reply_to_external_id: str | None = None
    comment_id: str | None = None  # public/private comment replies
    template: dict[str, Any] | None = None  # set by policy when a template is required outside the window
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Credentials:
    token: str
    account_id: str  # phone_number_id / page id / IG account id / bot id / simulator connection key


class ChannelAdapter(Protocol):
    provider: ChannelProvider
    manifest: Manifest

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> bool: ...

    def dedupe_key(self, raw_body: bytes, payload: dict[str, Any]) -> str: ...

    def route_key(self, payload: dict[str, Any], path_params: Mapping[str, str]) -> str | None: ...

    def parse(self, payload: dict[str, Any], route_key: str) -> list[NormalizedEvent]: ...

    def to_payloads(self, parts: list[IRPart], ctx: RenderContext) -> list[dict[str, Any]]: ...

    async def send(self, payload: dict[str, Any], creds: Credentials) -> SendResult: ...

    async def mark_read_and_typing(
        self, creds: Credentials, recipient: str, external_message_id: str | None
    ) -> None: ...

    async def download_media(self, creds: Credentials, media: InboundMedia) -> tuple[bytes, str]: ...
