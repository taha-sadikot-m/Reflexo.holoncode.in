"""Channel adapters: signature verification, webhook parsing, payload rendering, send classification (POC_03 §2, §10).

Ports and extends the old Meta MCP tests (harvest H15: test_meta_signature, test_meta_webhook_parsers,
test_meta_send_payloads, test_meta_token_errors) against the new stateless adapters.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from reflexo.common.blocks import Option, ProductView, VariantLine
from reflexo.common.contracts import SendStatus
from reflexo.common.ids import new_id
from reflexo.gateway.adapters import simulator
from reflexo.gateway.adapters.base import (
    Credentials,
    IRCards,
    IRChoices,
    IRImage,
    IRLink,
    IRText,
    RenderContext,
)
from reflexo.gateway.adapters.meta_common import classify_response, sign, verify_signature
from reflexo.gateway.adapters.meta_messaging import InstagramAdapter, MessengerAdapter
from reflexo.gateway.adapters.meta_whatsapp import WhatsAppAdapter
from reflexo.gateway.adapters.simulator import SimulatorAdapter
from reflexo.gateway.adapters.telegram import TelegramAdapter, to_html

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "meta"
SECRET = "test_meta_app_secret"
CREDS = Credentials(token="tok", account_id="PNID_1")


def fixture(name: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def opts(n: int) -> list[Option]:
    return [Option(id=f"o{i}", title=f"Option number {i} with a long title") for i in range(1, n + 1)]


# ---------------------------------------------------------------- signatures


class TestSignatures:
    def test_meta_valid_signature(self) -> None:
        body = b'{"object":"page"}'
        assert verify_signature(body, sign(body, SECRET), SECRET)

    def test_meta_rejects_tampered_body(self) -> None:
        assert not verify_signature(b'{"object":"x"}', sign(b'{"object":"page"}', SECRET), SECRET)

    @pytest.mark.parametrize("header", [None, "", "sha1=abc", "sha256=deadbeef"])
    def test_meta_rejects_missing_or_malformed_header(self, header: str | None) -> None:
        assert not verify_signature(b"{}", header, SECRET)

    def test_meta_rejects_when_secret_unset(self) -> None:
        body = b"{}"
        assert not verify_signature(body, sign(body, ""), "")

    @pytest.mark.parametrize("adapter_cls", [WhatsAppAdapter, MessengerAdapter, InstagramAdapter])
    def test_meta_adapters_use_app_secret(self, adapter_cls: type) -> None:
        body = json.dumps(fixture("wa_text")).encode()
        adapter = adapter_cls()
        assert adapter.verify({"x-hub-signature-256": sign(body, SECRET)}, body)
        assert not adapter.verify({"x-hub-signature-256": sign(body, "other")}, body)

    def test_telegram_secret_token(self) -> None:
        adapter = TelegramAdapter()
        assert adapter.verify({"x-telegram-bot-api-secret-token": "test_tg_secret"}, b"{}")
        assert not adapter.verify({"x-telegram-bot-api-secret-token": "wrong"}, b"{}")
        assert not adapter.verify({}, b"{}")

    def test_simulator_hmac(self) -> None:
        body = b'{"text":"hi"}'
        adapter = SimulatorAdapter()
        assert adapter.verify({"x-reflexo-signature": simulator.sign(body)}, body)
        assert not adapter.verify({"x-reflexo-signature": simulator.sign(b"other")}, body)
        assert not adapter.verify({}, body)

    def test_simulator_fails_closed_without_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from reflexo.common.config import get_settings

        monkeypatch.setenv("REFLEXO_SIMULATOR_SECRET", "")
        get_settings.cache_clear()
        try:
            body = b"{}"
            assert not SimulatorAdapter().verify({"x-reflexo-signature": simulator.sign(body)}, body)
        finally:
            monkeypatch.undo()
            get_settings.cache_clear()


# ---------------------------------------------------------------- WhatsApp parsing


class TestWhatsAppParse:
    a = WhatsAppAdapter()

    def parse(self, name: str) -> list[Any]:
        payload = fixture(name)
        return self.a.parse(payload, self.a.route_key(payload, {}) or "")

    def test_route_key_is_phone_number_id(self) -> None:
        assert self.a.route_key(fixture("wa_text"), {}) == "PNID_1"

    def test_text(self) -> None:
        [ev] = self.parse("wa_text")
        assert ev.kind == "message"
        assert ev.text == "Hi, do you have chocolate cake?"
        assert ev.sender_external_id == "+919876543210"
        assert ev.thread_key == "919876543210"
        assert ev.sender_display_name == "Rahul"
        assert ev.external_message_id == "wamid.TEXT1"
        assert ev.ends_thought  # ends with "?"

    def test_image_with_caption(self) -> None:
        [ev] = self.parse("wa_image")
        assert ev.text == "Can you make this?"
        assert ev.media[0].provider_media_id == "media_1"
        assert ev.media[0].kind == "image"
        assert ev.media[0].mime == "image/jpeg"

    @pytest.mark.parametrize(
        ("name", "option_id", "title"),
        [("wa_interactive_button", "size_1kg", "1 kg"), ("wa_list_reply", "flavour_choc", "Chocolate")],
    )
    def test_interactive_replies(self, name: str, option_id: str, title: str) -> None:
        [ev] = self.parse(name)
        assert ev.kind == "interactive_reply"
        assert ev.interactive is not None
        assert (ev.interactive.option_id, ev.interactive.title) == (option_id, title)

    def test_location(self) -> None:
        [ev] = self.parse("wa_location")
        assert ev.location is not None
        assert ev.location.latitude == pytest.approx(19.07)
        assert ev.text == "Bandra West, Mumbai"

    def test_failed_status_carries_error(self) -> None:
        [ev] = self.parse("wa_status_failed")
        assert ev.kind == "status"
        assert ev.status is not None
        assert (ev.status.provider_message_id, ev.status.status, ev.status.error_code) == (
            "wamid.OUT1",
            "failed",
            "131047",
        )

    def test_read_status(self) -> None:
        [ev] = self.parse("wa_status_read")
        assert ev.status is not None
        assert ev.status.status == "read"
        assert ev.status.error_code is None

    def test_call_signal(self) -> None:
        [ev] = self.parse("wa_call_connect")
        assert ev.kind == "call"
        assert ev.call is not None
        assert (ev.call.call_id, ev.call.event, ev.call.sdp_type) == ("wacid.CALL1", "connect", "offer")
        assert ev.call.sdp is not None and ev.call.sdp.startswith("v=0")
        assert ev.sender_external_id == "+919876543210"

    def test_other_objects_ignored(self) -> None:
        assert self.a.parse(fixture("messenger_text"), "PNID_1") == []


# ---------------------------------------------------------------- Messenger / Instagram parsing


class TestMessagingParse:
    fb = MessengerAdapter()
    ig = InstagramAdapter()

    def test_messenger_text(self) -> None:
        [ev] = self.fb.parse(fixture("messenger_text"), "PAGE_1")
        assert (ev.kind, ev.channel, ev.thread_key, ev.text) == (
            "message",
            "facebook_messenger",
            "PSID_1",
            "Hello from Messenger",
        )
        assert self.fb.route_key(fixture("messenger_text"), {}) == "PAGE_1"

    def test_messenger_image_attachment(self) -> None:
        [ev] = self.fb.parse(fixture("messenger_image"), "PAGE_1")
        assert ev.media[0].kind == "image"
        assert ev.media[0].url == "https://cdn.example/img.jpg"

    def test_postback_and_quick_reply_are_interactive(self) -> None:
        [pb] = self.fb.parse(fixture("messenger_postback"), "PAGE_1")
        [qr] = self.fb.parse(fixture("messenger_quick_reply"), "PAGE_1")
        assert pb.interactive is not None and pb.interactive.option_id == "order:abc"
        assert qr.interactive is not None and qr.interactive.option_id == "confirm_yes"

    def test_echo_from_page_inbox(self) -> None:
        [ev] = self.fb.parse(fixture("messenger_echo_human"), "PAGE_1")
        assert ev.kind == "echo"
        assert ev.thread_key == "PSID_1"  # the customer, not the page
        assert ev.echo_app_id == "263902037430900"

    def test_feed_non_comment_filtered(self) -> None:  # ported: test_process_facebook_comment_filters_non_comments
        assert self.fb.parse(fixture("page_feed_post"), "PAGE_1") == []

    def test_feed_comment(self) -> None:
        [ev] = self.fb.parse(fixture("page_feed_comment"), "PAGE_1")
        assert (ev.kind, ev.channel, ev.thread_key, ev.sender_display_name) == (
            "comment",
            "facebook_comment",
            "POST_1_C1",
            "Asha",
        )
        assert ev.comment is not None and ev.comment.post_id == "PAGE_1_POST_1"

    def test_instagram_dm(self) -> None:  # ported: test_process_instagram_entry_messaging
        [ev] = self.ig.parse(fixture("ig_dm"), "IG_1")
        assert (ev.kind, ev.channel, ev.text, ev.thread_key) == ("message", "instagram_dm", "Hello IG", "IGSID_1")

    def test_instagram_comment(self) -> None:
        [ev] = self.ig.parse(fixture("ig_comment"), "IG_1")
        assert (ev.kind, ev.channel, ev.thread_key, ev.sender_external_id) == (
            "comment",
            "instagram_comment",
            "IGC_1",
            "IGSID_2",
        )
        assert ev.comment is not None and ev.comment.post_id == "MEDIA_1"

    def test_instagram_own_comment_ignored(self) -> None:
        assert self.ig.parse(fixture("ig_comment_own"), "IG_1") == []

    def test_wrong_object_ignored(self) -> None:
        assert self.ig.parse(fixture("messenger_text"), "IG_1") == []


# ---------------------------------------------------------------- Telegram parsing


class TestTelegramParse:
    a = TelegramAdapter()

    def test_text(self) -> None:
        [ev] = self.a.parse(fixture("telegram_text"), "conn")
        assert (ev.thread_key, ev.sender_external_id, ev.sender_display_name) == ("5551", "5551", "Rahul S")
        assert self.a.dedupe_key(b"", fixture("telegram_text")) == "9001"
        assert self.a.route_key({}, {"connection_id": "abc"}) == "abc"

    def test_start_payload_is_referral(self) -> None:
        [ev] = self.a.parse(fixture("telegram_start"), "conn")
        assert ev.referral_code == "insta_diwali"

    def test_photo_takes_largest_size(self) -> None:
        [ev] = self.a.parse(fixture("telegram_photo"), "conn")
        assert ev.media[0].provider_media_id == "big"
        assert ev.text == "this one"

    def test_callback_query(self) -> None:
        [ev] = self.a.parse(fixture("telegram_callback"), "conn")
        assert ev.kind == "interactive_reply"
        assert ev.interactive is not None and ev.interactive.option_id == "size_1kg"
        assert ev.raw["callback_query_id"] == "cbq1"


# ---------------------------------------------------------------- egress payloads


CTX = RenderContext(recipient="919876543210")


class TestPayloads:
    def test_whatsapp_buttons_up_to_three(self) -> None:
        [p] = WhatsAppAdapter().to_payloads([IRChoices("Pick", opts(3), "buttons")], CTX)
        buttons = p["interactive"]["action"]["buttons"]
        assert p["interactive"]["type"] == "button"
        assert len(buttons) == 3
        assert all(len(b["reply"]["title"]) <= 20 for b in buttons)
        assert p["to"] == "919876543210" and p["messaging_product"] == "whatsapp"

    def test_whatsapp_list_rows_capped(self) -> None:
        [p] = WhatsAppAdapter().to_payloads([IRChoices("Pick", opts(12), "list", "View cakes")], CTX)
        rows = p["interactive"]["action"]["sections"][0]["rows"]
        assert p["interactive"]["type"] == "list"
        assert len(rows) == 10
        assert all(len(r["title"]) <= 24 for r in rows)
        assert p["interactive"]["action"]["button"] == "View cakes"

    def test_whatsapp_reply_context_on_first_part_only(self) -> None:
        ctx = RenderContext(recipient="1", reply_to_external_id="wamid.IN")
        payloads = WhatsAppAdapter().to_payloads([IRText("a"), IRText("b")], ctx)
        assert payloads[0]["context"] == {"message_id": "wamid.IN"}
        assert "context" not in payloads[1]

    def test_whatsapp_template_replaces_parts(self) -> None:
        ctx = RenderContext(recipient="1", template={"name": "order_update", "language": "en", "vars": ["Ready!"]})
        [p] = WhatsAppAdapter().to_payloads([IRText("ignored"), IRText("also")], ctx)
        assert p["type"] == "template"
        assert p["template"]["name"] == "order_update"
        assert p["template"]["components"][0]["parameters"] == [{"type": "text", "text": "Ready!"}]

    def test_whatsapp_image_caption(self) -> None:
        [p] = WhatsAppAdapter().to_payloads([IRImage("https://x/i.jpg", "Choco")], CTX)
        assert p["image"] == {"link": "https://x/i.jpg", "caption": "Choco"}

    def test_messenger_button_template_and_link(self) -> None:
        a = MessengerAdapter()
        ctx = RenderContext(recipient="PSID_1")
        [b] = a.to_payloads([IRChoices("Pick", opts(3), "buttons")], ctx)
        [link] = a.to_payloads([IRLink("Pay ₹500", "https://rzp.io/x", "Pay now")], ctx)
        assert b["message"]["attachment"]["payload"]["template_type"] == "button"
        assert b["recipient"] == {"id": "PSID_1"}
        assert link["message"]["attachment"]["payload"]["buttons"][0] == {
            "type": "web_url",
            "url": "https://rzp.io/x",
            "title": "Pay now",
        }

    def test_messenger_comment_reply_uses_comment_id(self) -> None:
        [p] = MessengerAdapter().to_payloads([IRText("Sent you a DM!")], RenderContext(recipient="x", comment_id="C1"))
        assert p["recipient"] == {"comment_id": "C1"}

    def test_messenger_carousel(self) -> None:
        view = ProductView(
            offering_id=new_id(),
            name="Chocolate Truffle",
            lines=[VariantLine(variant_id=None, name="1 kg", price_display="₹900")],
            image_url="https://x/c.jpg",
        )
        [p] = MessengerAdapter().to_payloads([IRCards([view])], RenderContext(recipient="PSID_1"))
        element = p["message"]["attachment"]["payload"]["elements"][0]
        assert element["title"] == "Chocolate Truffle"
        assert element["image_url"] == "https://x/c.jpg"
        assert element["buttons"][0]["payload"] == f"order:{view.offering_id}"

    def test_instagram_quick_replies(self) -> None:
        [p] = InstagramAdapter().to_payloads([IRChoices("Pick", opts(5), "quick_replies")], RenderContext("IGSID"))
        assert len(p["message"]["quick_replies"]) == 5
        assert p["message"]["text"] == "Pick"

    def test_telegram_html_escaping_and_bold(self) -> None:
        assert to_html("*Choco* <cake> & more") == "<b>Choco</b> &lt;cake&gt; &amp; more"
        [p] = TelegramAdapter().to_payloads([IRText("1 < 2")], RenderContext(recipient="5551"))
        assert p == {"method": "sendMessage", "chat_id": "5551", "text": "1 &lt; 2", "parse_mode": "HTML"}

    def test_telegram_callback_data_within_64_bytes(self) -> None:
        long_id = "x" * 48
        [p] = TelegramAdapter().to_payloads(
            [IRChoices("Pick", [Option(id=long_id, title="T")], "buttons")], RenderContext("1")
        )
        data = p["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert data == f"o:{long_id}"
        assert len(data.encode()) <= 64


# ---------------------------------------------------------------- send classification


def _resp(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


class TestSendClassification:
    @pytest.mark.parametrize(
        ("status", "body", "expected_status", "code"),
        [
            (200, {"messages": [{"id": "wamid.OK"}]}, SendStatus.SENT, None),
            (401, {"error": {"code": 190, "message": "expired"}}, SendStatus.PERMANENT, "CREDENTIAL_INVALID"),
            (400, {"error": {"code": 131005, "message": "denied"}}, SendStatus.PERMANENT, "CREDENTIAL_INVALID"),
            (400, {"error": {"code": 131047, "message": "window"}}, SendStatus.PERMANENT, "WINDOW_CLOSED"),
            (400, {"error": {"code": 130429, "message": "rate"}}, SendStatus.RETRYABLE, "RATE_LIMITED"),
            (429, {"error": {"code": 4}}, SendStatus.RETRYABLE, "RATE_LIMITED"),
            (503, {"error": {"code": 2}}, SendStatus.RETRYABLE, "PROVIDER_UNAVAILABLE"),
            (400, {"error": {"code": 100, "message": "bad param"}}, SendStatus.PERMANENT, "PROVIDER_ERROR"),
        ],
    )
    def test_meta_codes(self, status: int, body: dict[str, Any], expected_status: SendStatus, code: str | None) -> None:
        result = classify_response(_resp(status, body), ("messages", 0, "id"))
        assert result.status == expected_status
        assert result.error_code == code
        if expected_status == SendStatus.SENT:
            assert result.provider_message_id == "wamid.OK"

    def test_retry_after_header_respected(self) -> None:
        result = classify_response(_resp(429, {}, {"Retry-After": "12"}), ("x",))
        assert result.retry_after_s == 12.0

    async def test_whatsapp_send_posts_with_bearer(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _resp(200, {"messages": [{"id": "wamid.SENT"}]})

        result = await WhatsAppAdapter(mock_client(handler)).send({"to": "1", "type": "text"}, CREDS)
        assert result.status == SendStatus.SENT and result.provider_message_id == "wamid.SENT"
        assert seen[0].headers["authorization"] == "Bearer tok"
        assert seen[0].url.path.endswith("/PNID_1/messages")

    async def test_read_timeout_is_unknown_outcome(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        result = await WhatsAppAdapter(mock_client(handler)).send({}, CREDS)
        assert result.status == SendStatus.UNKNOWN
        assert result.retry_after_unknown_allowed

    async def test_connect_error_is_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        result = await MessengerAdapter(mock_client(handler)).send({}, CREDS)
        assert result.status == SendStatus.RETRYABLE

    async def test_telegram_send_and_errors(self) -> None:
        responses = iter(
            [
                _resp(200, {"ok": True, "result": {"message_id": 77}}),
                _resp(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
                _resp(403, {"ok": False, "description": "bot was blocked by the user"}),
            ]
        )
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return next(responses)

        a = TelegramAdapter(mock_client(handler))
        creds = Credentials(token="123:tg", account_id="")
        ok = await a.send({"method": "sendMessage", "chat_id": "1", "text": "hi"}, creds)
        limited = await a.send({"method": "sendMessage", "chat_id": "1", "text": "hi"}, creds)
        blocked = await a.send({"method": "sendMessage", "chat_id": "1", "text": "hi"}, creds)
        assert ok.status == SendStatus.SENT and ok.provider_message_id == "77"
        assert limited.status == SendStatus.RETRYABLE and limited.retry_after_s == 3.0
        assert blocked.error_code == "RECIPIENT_UNREACHABLE"
        assert urls[0].endswith("/bot123:tg/sendMessage")

    async def test_telegram_timeout_forbids_blind_retry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        result = await TelegramAdapter(mock_client(handler)).send({"method": "sendMessage"}, CREDS)
        assert result.status == SendStatus.UNKNOWN
        assert not result.retry_after_unknown_allowed
