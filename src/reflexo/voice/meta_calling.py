"""WhatsApp Calling Graph actions (harvested H7 from the old `voice/meta_api.py`).

`pre_accept`, `accept` and `terminate` with retries + backoff for transient failures (connect errors, 5xx, 429),
never for client errors; Graph codes mapped to typed errors; tokens never logged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from reflexo.common.config import get_settings
from reflexo.common.logging import get_logger
from reflexo.voice.sdp import VoiceSdpError

log = get_logger(__name__)
TIMEOUT = httpx.Timeout(8.0, connect=3.0)
RETRY_DELAYS_S = (0.3, 0.8)  # 3 attempts in total; the caller is ringing, so keep it short

# Graph error codes for WhatsApp Calling that no retry will fix (subset kept from the old repo).
CALL_ERRORS: dict[int, str] = {
    138000: "Calling is not enabled on this WhatsApp number.",
    138001: "This number cannot receive WhatsApp calls.",
    138003: "A call with this customer is already in progress.",
    138005: "WhatsApp call rate limit reached.",
    138006: "No approved call permission for this customer.",
    138008: "Meta rejected the SDP.",
    138012: "Daily business-initiated call limit reached for this customer.",
}


class CallGraphError(Exception):
    def __init__(
        self, action: str, message: str, *, status: int | None = None, code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(f"{action}: {message}")
        self.action = action
        self.status = status
        self.code = code
        self.retryable = retryable


class MetaCalling:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)

    def _url(self, phone_number_id: str) -> str:
        s = get_settings()
        return f"{s.meta_graph_base}/{s.meta_graph_version}/{phone_number_id}/calls"

    async def _post(self, action: str, phone_number_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        last: CallGraphError | None = None
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                resp = await self._client.post(
                    self._url(phone_number_id), json=body, headers={"Authorization": f"Bearer {token}"}
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last = CallGraphError(action, type(exc).__name__, retryable=True)
            else:
                if resp.status_code < 300:
                    data: dict[str, Any] = resp.json() if resp.content else {}
                    log.info("call_graph_ok", action=action, call_id=body.get("call_id"), attempt=attempt + 1)
                    return data
                try:
                    err = resp.json().get("error", {})
                except ValueError:
                    err = {}
                code = err.get("code")
                message = CALL_ERRORS.get(code, str(err.get("message") or resp.text[:200]))
                retryable = resp.status_code >= 500 or resp.status_code == 429
                last = CallGraphError(action, message, status=resp.status_code, code=code, retryable=retryable)
                if code == 138008:
                    raise VoiceSdpError(f"{action}: {message}") from last
            log.warning(
                "call_graph_failed",
                action=action,
                call_id=body.get("call_id"),
                attempt=attempt + 1,
                status=last.status,
                code=last.code,
                retryable=last.retryable,
            )
            if not last.retryable or attempt == len(RETRY_DELAYS_S):
                break
            await asyncio.sleep(RETRY_DELAYS_S[attempt])
        assert last is not None
        raise last

    async def pre_accept(self, phone_number_id: str, token: str, call_id: str, sdp_answer: str) -> dict[str, Any]:
        """Send our SDP answer early so media can connect while the agent joins (the caller still hears ringing)."""
        return await self._post(
            "pre_accept",
            phone_number_id,
            token,
            {
                "messaging_product": "whatsapp",
                "call_id": call_id,
                "action": "pre_accept",
                "session": {"sdp_type": "answer", "sdp": sdp_answer},
            },
        )

    async def accept(self, phone_number_id: str, token: str, call_id: str, sdp_answer: str) -> dict[str, Any]:
        return await self._post(
            "accept",
            phone_number_id,
            token,
            {
                "messaging_product": "whatsapp",
                "call_id": call_id,
                "action": "accept",
                "session": {"sdp_type": "answer", "sdp": sdp_answer},
            },
        )

    async def terminate(self, phone_number_id: str, token: str, call_id: str) -> dict[str, Any]:
        return await self._post(
            "terminate",
            phone_number_id,
            token,
            {
                "messaging_product": "whatsapp",
                "call_id": call_id,
                "action": "terminate",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()
