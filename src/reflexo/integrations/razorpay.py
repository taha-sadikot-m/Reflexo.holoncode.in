"""Razorpay (POC_05 §4.5): payment links and webhook verification. Test-mode keys from settings (POC).

Amounts always come from the caller (commerce computes them from order rows); the model never supplies one.
`reference_id` is unique at Razorpay, so a create retried after a crash cannot produce a second link.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import httpx

from reflexo.common.config import get_settings
from reflexo.common.logging import get_logger

log = get_logger(__name__)
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
RETRY_DELAYS_S = (0.5, 1.5)
# Most a request may wait between its retries. A customer is waiting for the reply; a limit that asks for longer
# fails the request at once, so the caller can say so instead of holding the turn.
RETRY_WAIT_BUDGET_S = 6.0


def _retry_after_s(resp: httpx.Response) -> float | None:
    """Razorpay's own wait on a 429, if it sends one (seconds form of Retry-After)."""
    try:
        return max(0.0, float(resp.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


class RazorpayError(Exception):
    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class PaymentLink:
    id: str
    short_url: str
    status: str  # created | partially_paid | paid | expired | cancelled
    amount: int
    reference_id: str | None
    payment_id: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> PaymentLink:
        payments = d.get("payments") or []
        paid = next((p for p in payments if p.get("status") == "captured"), None)
        return cls(
            id=str(d["id"]),
            short_url=str(d.get("short_url") or ""),
            status=str(d.get("status") or ""),
            amount=int(d.get("amount") or 0),
            reference_id=d.get("reference_id"),
            payment_id=(paid or {}).get("payment_id"),
        )


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """X-Razorpay-Signature = hex HMAC-SHA256 of the raw body with the webhook secret; constant-time compare."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign(raw_body: bytes, secret: str) -> str:
    """Build a valid signature (Simulation Lab "simulate payment", tests)."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


class RazorpayClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        s = get_settings()
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)
        self._auth = (s.razorpay_key_id, s.razorpay_key_secret.get_secret_value())
        self._base = s.razorpay_api_base

    @property
    def configured(self) -> bool:
        return bool(self._auth[0] and self._auth[1])

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise RazorpayError("Razorpay keys are not configured", code="CREDENTIAL_INVALID")
        last: RazorpayError | None = None
        waited = 0.0
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            wait: float | None = RETRY_DELAYS_S[attempt] if attempt < len(RETRY_DELAYS_S) else None
            try:
                resp = await self._client.request(method, f"{self._base}{path}", json=json, auth=self._auth)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last = RazorpayError(type(exc).__name__, code="PROVIDER_UNAVAILABLE", retryable=True)
            else:
                if resp.status_code < 300:
                    data: dict[str, Any] = resp.json()
                    return data
                err = (
                    (resp.json() if resp.content else {}).get("error", {})
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                retryable = resp.status_code >= 500 or resp.status_code == 429
                if resp.status_code == 401:
                    code = "CREDENTIAL_INVALID"
                elif resp.status_code == 429:
                    code = "RATE_LIMITED"
                    asked = _retry_after_s(resp)
                    wait = max(wait, asked) if wait is not None and asked is not None else wait
                else:
                    code = str(err.get("code") or "PROVIDER_ERROR")
                last = RazorpayError(
                    str(err.get("description") or resp.text[:200]),
                    status=resp.status_code,
                    code=code,
                    retryable=retryable,
                )
            log.warning("razorpay_request_failed", path=path, attempt=attempt + 1, code=last.code, status=last.status)
            if not last.retryable or wait is None or waited + wait > RETRY_WAIT_BUDGET_S:
                break
            await asyncio.sleep(wait)
            waited += wait
        assert last is not None
        raise last

    async def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        description: str,
        expire_by: int,
        customer_name: str | None,
        customer_contact: str | None,
        notes: dict[str, str],
    ) -> PaymentLink:
        if amount_paise <= 0:
            raise RazorpayError("amount must be positive", code="VALIDATION_FAILED")
        body: dict[str, Any] = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": description[:2048],
            "reference_id": reference_id,
            "expire_by": expire_by,
            "reminder_enable": False,
            "notify": {"sms": False, "email": False},
            "notes": notes,
        }
        customer = {k: v for k, v in (("name", customer_name), ("contact", customer_contact)) if v}
        if customer:
            body["customer"] = customer
        try:
            return PaymentLink.from_api(await self._request("POST", "/payment_links", body))
        except RazorpayError as exc:
            # a retried create after a crash: Razorpay rejects the duplicate reference_id -> fetch the existing link
            if exc.status == 400 and "reference_id" in str(exc).lower():
                existing = await self.find_by_reference(reference_id)
                if existing is not None:
                    return existing
            raise

    async def find_by_reference(self, reference_id: str) -> PaymentLink | None:
        data = await self._request("GET", f"/payment_links?reference_id={reference_id}")
        links = data.get("payment_links") or []
        return PaymentLink.from_api(links[0]) if links else None

    async def fetch_payment_link(self, link_id: str) -> PaymentLink:
        return PaymentLink.from_api(await self._request("GET", f"/payment_links/{link_id}"))

    async def refund_payment(self, payment_id: str, amount_paise: int, notes: dict[str, str]) -> dict[str, Any]:
        """Refund a captured payment (full or partial). Returns Razorpay's refund entity (id, amount, status)."""
        if amount_paise <= 0:
            raise RazorpayError("amount must be positive", code="VALIDATION_FAILED")
        return await self._request("POST", f"/payments/{payment_id}/refund", {"amount": amount_paise, "notes": notes})

    async def cancel_payment_link(self, link_id: str) -> PaymentLink:
        """Only an unpaid (`created`) link can be cancelled; Razorpay answers 400 otherwise."""
        return PaymentLink.from_api(await self._request("POST", f"/payment_links/{link_id}/cancel"))

    async def aclose(self) -> None:
        await self._client.aclose()


SIMULATED_LINK_PREFIX = "plink_sim_"


class SimulatedRazorpay:
    """Payment links for simulated conversations (Simulation Lab, scenarios, benchmark, the public demo).

    Their payments are always simulated (a signed `payment_link.paid` webhook from the Lab's Pay button), so a real
    link only spent the account's links: a Razorpay test account makes 30 in its whole life, and on 14 Oct the
    showcase hit that limit and every order fell back. Same interface as `RazorpayClient`; nothing leaves the
    server. A link is "created" until the simulator pays it or its own expiry passes.
    """

    configured = True

    def _link(self, reference_id: str, amount: int, status: str = "created") -> PaymentLink:
        return PaymentLink(
            id=f"{SIMULATED_LINK_PREFIX}{reference_id.replace('-', '')}",
            short_url=f"{get_settings().public_base_url.rstrip('/')}/integrations/razorpay/simulated/{reference_id}",
            status=status,
            amount=amount,
            reference_id=reference_id,
        )

    async def create_payment_link(self, *, amount_paise: int, reference_id: str, **_: Any) -> PaymentLink:
        if amount_paise <= 0:
            raise RazorpayError("amount must be positive", code="VALIDATION_FAILED")
        return self._link(reference_id, amount_paise)

    async def find_by_reference(self, reference_id: str) -> PaymentLink | None:
        return self._link(reference_id, 0)

    async def fetch_payment_link(self, link_id: str) -> PaymentLink:
        return self._link(_reference_of(link_id), 0)

    async def cancel_payment_link(self, link_id: str) -> PaymentLink:
        return self._link(_reference_of(link_id), 0, status="cancelled")

    async def refund_payment(self, payment_id: str, amount_paise: int, notes: dict[str, str]) -> dict[str, Any]:
        return {"id": f"rfnd_sim_{payment_id[-14:]}", "amount": amount_paise, "status": "processed"}


def _reference_of(link_id: str) -> str:
    raw = link_id.removeprefix(SIMULATED_LINK_PREFIX)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}" if len(raw) == 32 else raw
