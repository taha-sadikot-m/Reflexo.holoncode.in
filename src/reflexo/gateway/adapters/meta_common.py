"""Shared Meta pieces: webhook signature verification (harvested H1) and a Graph API client whose errors are
classified for the outbound pipeline (harvested from the old graph_client, H4).

Classification rules (POC_03 §7.3–7.4):
* connect failure / connect timeout → RETRYABLE (request never reached Meta)
* read/write timeout after sending   → UNKNOWN (Meta may have accepted it)
* HTTP 429 or throttling codes        → RETRYABLE with backoff
* HTTP 5xx                            → RETRYABLE
* auth codes                          → PERMANENT CREDENTIAL_INVALID
* 131047 (outside 24 h window)        → PERMANENT WINDOW_CLOSED
* other 4xx                           → PERMANENT PROVIDER_ERROR
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from reflexo.common.contracts import SendResult, SendStatus
from reflexo.common.logging import get_logger

log = get_logger(__name__)

AUTH_ERROR_CODES = frozenset({190, 102, 463, 467, 131005})
THROTTLE_CODES = frozenset({4, 17, 32, 613, 80007, 130429, 131056})
WINDOW_CODES = frozenset({131047})
TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """X-Hub-Signature-256: sha256=<hex>; constant-time compare. Missing secret or header → False."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[7:])


def sign(raw_body: bytes, app_secret: str) -> str:
    """Build a valid signature header (Simulation Lab and tests)."""
    return "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def wa_id_to_e164(wa_id: str) -> str:
    digits = "".join(ch for ch in str(wa_id) if ch.isdigit())
    return f"+{digits}" if digits else ""


def e164_to_wa(e164: str) -> str:
    return e164.lstrip("+")


def classify_response(resp: httpx.Response, id_path: tuple[str | int, ...]) -> SendResult:
    if resp.status_code < 300:
        data: Any = resp.json()
        try:
            for key in id_path:
                data = data[key]
        except (KeyError, IndexError, TypeError):
            data = None
        return SendResult(SendStatus.SENT, provider_message_id=str(data) if data else None)
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    code = err.get("code")
    message = str(err.get("message") or resp.text[:300])
    retry_after = float(resp.headers.get("Retry-After", 0) or 0) or None
    if resp.status_code == 429 or code in THROTTLE_CODES:
        return SendResult(
            SendStatus.RETRYABLE, error_code="RATE_LIMITED", error_message=message, retry_after_s=retry_after or 5.0
        )
    if resp.status_code >= 500:
        return SendResult(SendStatus.RETRYABLE, error_code="PROVIDER_UNAVAILABLE", error_message=message)
    if code in AUTH_ERROR_CODES or resp.status_code == 401:
        return SendResult(SendStatus.PERMANENT, error_code="CREDENTIAL_INVALID", error_message=message)
    if code in WINDOW_CODES:
        return SendResult(SendStatus.PERMANENT, error_code="WINDOW_CLOSED", error_message=message)
    return SendResult(SendStatus.PERMANENT, error_code="PROVIDER_ERROR", error_message=f"({code}) {message}")


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    token: str,
    id_path: tuple[str | int, ...],
    use_query_token: bool = False,
) -> SendResult:
    headers = {} if use_query_token else {"Authorization": f"Bearer {token}"}
    params = {"access_token": token} if use_query_token else None
    try:
        resp = await client.post(url, json=payload, headers=headers, params=params, timeout=TIMEOUT)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        return SendResult(SendStatus.RETRYABLE, error_code="PROVIDER_UNAVAILABLE", error_message=type(exc).__name__)
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
        return SendResult(SendStatus.UNKNOWN, error_code="TIMEOUT", error_message=type(exc).__name__)
    return classify_response(resp, id_path)
