"""Razorpay client (POC_05 §4.5): signature verification, retries, duplicate-reference recovery, error mapping."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from reflexo.common.config import get_settings
from reflexo.integrations import razorpay
from reflexo.integrations.razorpay import PaymentLink, RazorpayClient, RazorpayError, sign, verify_signature

LINK = {
    "id": "plink_A",
    "short_url": "https://rzp.io/rzp/A",
    "status": "created",
    "amount": 95000,
    "reference_id": "ref-1",
}


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("REFLEXO_RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("REFLEXO_RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(razorpay, "RETRY_DELAYS_S", (0.0, 0.0))
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def client(handler: httpx.MockTransport) -> RazorpayClient:
    return RazorpayClient(httpx.AsyncClient(transport=handler))


async def create(c: RazorpayClient) -> PaymentLink:
    return await c.create_payment_link(
        amount_paise=95000,
        reference_id="ref-1",
        description="Order MB-1001",
        expire_by=1_900_000_000,
        customer_name="Rahul",
        customer_contact="+919811122233",
        notes={"order": "MB-1001"},
    )


def test_signature_roundtrip_and_tamper() -> None:
    body = b'{"event":"payment_link.paid"}'
    sig = sign(body, "whsec")
    assert verify_signature(body, sig, "whsec")
    assert not verify_signature(body + b" ", sig, "whsec")
    assert not verify_signature(body, sig, "other")
    assert not verify_signature(body, None, "whsec") and not verify_signature(body, sig, "")


def test_payment_id_comes_from_captured_payment() -> None:
    link = PaymentLink.from_api(
        {
            **LINK,
            "status": "paid",
            "payments": [
                {"payment_id": "pay_failed", "status": "failed"},
                {"payment_id": "pay_ok", "status": "captured"},
            ],
        }
    )
    assert (link.status, link.payment_id) == ("paid", "pay_ok")


async def test_create_sends_server_amount_and_retries_5xx() -> None:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=LINK)

    link = await create(client(httpx.MockTransport(handle)))
    assert link.id == "plink_A" and len(calls) == 2
    body = json.loads(calls[-1].content)
    assert body["amount"] == 95000 and body["currency"] == "INR" and body["reference_id"] == "ref-1"
    assert body["accept_partial"] is False and body["customer"]["contact"] == "+919811122233"
    assert calls[-1].headers["authorization"].startswith("Basic ")


async def test_rate_limit_waits_as_razorpay_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """WP-00 (live 14 Oct, 429 on /payment_links): the retry waits at least Retry-After, not our own short delay."""
    monkeypatch.setattr(razorpay, "RETRY_DELAYS_S", (0.5, 1.5))
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(razorpay.asyncio, "sleep", sleep)
    calls = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"description": "Too many"}})
        return httpx.Response(200, json=LINK)

    assert (await create(client(httpx.MockTransport(handle)))).id == "plink_A"
    assert slept == [2.0]


async def test_rate_limit_longer_than_the_wait_budget_fails_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(razorpay.asyncio, "sleep", sleep)
    limited = httpx.Response(429, headers={"retry-after": "60"}, json={"error": {"description": "Too many"}})
    with pytest.raises(RazorpayError) as err:
        await create(client(httpx.MockTransport(lambda r: limited)))
    assert (err.value.code, err.value.status, slept) == ("RATE_LIMITED", 429, [])


async def test_simulated_links_round_trip_without_any_request() -> None:
    from reflexo.integrations.api import razorpay as provider

    sim = provider(simulated=True)
    assert isinstance(sim, razorpay.SimulatedRazorpay) and not isinstance(provider(), razorpay.SimulatedRazorpay)
    ref = "01a1396e-f375-708b-9963-f0b0f8fa680c"
    link = await sim.create_payment_link(amount_paise=95000, reference_id=ref, description="d", expire_by=1)
    assert link.id == razorpay.SIMULATED_LINK_PREFIX + ref.replace("-", "") and link.short_url.endswith(ref)
    assert (await sim.fetch_payment_link(link.id)).reference_id == ref
    assert (await sim.cancel_payment_link(link.id)).status == "cancelled"
    assert (await sim.refund_payment("pay_SIM123", 95000, {}))["amount"] == 95000
    with pytest.raises(RazorpayError):
        await sim.create_payment_link(amount_paise=0, reference_id=ref)


async def test_duplicate_reference_fetches_existing_link() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(
                400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "reference_id already exists"}}
            )
        assert req.url.params["reference_id"] == "ref-1"
        return httpx.Response(200, json={"payment_links": [LINK]})

    assert (await create(client(httpx.MockTransport(handle)))).id == "plink_A"


async def test_auth_failure_is_not_retried() -> None:
    calls = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "auth failed"}})

    with pytest.raises(RazorpayError) as err:
        await create(client(httpx.MockTransport(handle)))
    assert (err.value.code, err.value.retryable, calls) == ("CREDENTIAL_INVALID", False, 1)


async def test_unreachable_provider_is_retryable() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    with pytest.raises(RazorpayError) as err:
        await client(httpx.MockTransport(handle)).fetch_payment_link("plink_A")
    assert (err.value.code, err.value.retryable) == ("PROVIDER_UNAVAILABLE", True)


async def test_missing_keys_and_bad_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RazorpayError, match="positive"):
        await client(httpx.MockTransport(lambda r: httpx.Response(200, json=LINK))).create_payment_link(
            amount_paise=0,
            reference_id="r",
            description="d",
            expire_by=1,
            customer_name=None,
            customer_contact=None,
            notes={},
        )
    monkeypatch.setenv("REFLEXO_RAZORPAY_KEY_SECRET", "")
    get_settings.cache_clear()
    with pytest.raises(RazorpayError) as err:
        await client(httpx.MockTransport(lambda r: httpx.Response(200, json=LINK))).fetch_payment_link("x")
    assert err.value.code == "CREDENTIAL_INVALID"
