"""voice-bridge call control on a real database with a fake WebRTC bridge and a mocked Graph API
(POC_01 §5.3, WP-12a done-when): accept order, idempotency, hangup during setup, agent timeout, agent end,
caller hangup, restart recovery, and the full webhook → inbox → `voice.call_signal` job path."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

import reflexo.gateway.api as gateway_api
import reflexo.voice.api as voice_api
from reflexo.common.contracts import JobQueuePort
from reflexo.common.db import Database, ServiceReason
from reflexo.common.ports import Registry, reset_registry
from reflexo.pipeline import api as pipeline_api
from reflexo.voice.bridge_worker import BridgeWorker
from reflexo.voice.call_controller import CallController
from reflexo.voice.meta_calling import MetaCalling
from tests.integration.test_gateway import OWNER_PHONE, deliver, make_shop

pytestmark = pytest.mark.integration


class FakeBridge:
    def __init__(self, call_id: str, *, agent_joins: bool = True) -> None:
        from reflexo.voice.bridge_webrtc import AudioMetrics

        self.call_id = call_id
        self.room_name = f"call-{call_id}"
        self.caller_audio_ended = asyncio.Event()
        self.metrics = AudioMetrics()
        self.agent_joins = agent_joins
        self.agent_here = True
        self.metadata: dict[str, Any] = {}
        self.closed = False
        self.join_gate = asyncio.Event()
        self.join_gate.set()

    async def create_sdp_answer(self, sdp_offer: str) -> str:
        return "v=0\r\nanswer\r\n"

    async def connect_livekit(
        self, url: str, api_key: str, api_secret: str, *, agent_name: str, metadata: dict[str, Any]
    ) -> None:
        self.metadata = metadata

    async def wait_for_agent(self, timeout_s: float) -> bool:
        await self.join_gate.wait()
        if not self.agent_joins:
            await asyncio.sleep(timeout_s)
            return False
        return True

    async def agent_present(self) -> bool:
        return self.agent_here

    async def close(self) -> None:
        self.closed = True


class Graph:
    """Mock Graph /calls endpoint recording actions."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.actions.append((body["action"], body["call_id"]))
        return httpx.Response(200, json={"success": True})


@pytest.fixture
def graph() -> Graph:
    return Graph()


@pytest.fixture
def bridges() -> dict[str, FakeBridge]:
    return {}


@pytest.fixture(autouse=True)
def _fast_voice(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from reflexo.common.config import get_settings

    monkeypatch.setenv("REFLEXO_VOICE_AGENT_JOIN_TIMEOUT_S", "0.3")
    monkeypatch.setattr("reflexo.voice.call_controller.WATCHDOG_S", 0.05)
    monkeypatch.setattr("reflexo.voice.call_controller.AGENT_GONE_GRACE_S", 0.1)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def controller(
    graph: Graph, bridges: dict[str, FakeBridge], *, host: str = "test-host", agent_joins: bool = True
) -> CallController:
    def factory(call_id: str) -> FakeBridge:
        b = FakeBridge(call_id, agent_joins=agent_joins)
        bridges[call_id] = b
        return b

    return CallController(
        host, meta=MetaCalling(httpx.AsyncClient(transport=httpx.MockTransport(graph.handler))), bridge_factory=factory
    )


def connect(conn_id: UUID, call_id: str, wa: str = "919833300001") -> dict[str, Any]:
    return {
        "business_id": "x",
        "connection_id": str(conn_id),
        "call": {
            "call_id": call_id,
            "event": "connect",
            "sdp": "v=0\r\noffer\r\n",
            "sdp_type": "offer",
            "caller_wa_id": wa,
            "raw": {},
        },
    }


def terminate(conn_id: UUID, call_id: str) -> dict[str, Any]:
    return {
        "connection_id": str(conn_id),
        "call": {"call_id": call_id, "event": "terminate", "raw": {"status": "COMPLETED"}},
    }


async def session_row(db: Database, call_id: str) -> Any:
    async with db.service_session(ServiceReason.ADMIN) as s:
        return (await s.execute(text("SELECT * FROM call_sessions WHERE provider_call_id = :p"), {"p": call_id})).one()


async def wait_until(pred: Any, limit_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + limit_s
    while loop.time() < end:
        if await pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------- happy path + idempotency


async def test_inbound_call_accepted_after_agent_ready(
    db: Database, graph: Graph, bridges: dict[str, FakeBridge]
) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges)
    await c.handle_signal(connect(shop.wa, "wacid.A"))

    assert graph.actions == [("pre_accept", "wacid.A"), ("accept", "wacid.A")]
    row = await session_row(db, "wacid.A")
    assert (str(row.status), row.livekit_room, row.bridge_host, row.caller_e164) == (
        "active",
        "call-wacid.A",
        "test-host",
        "+919833300001",
    )
    assert row.connected_at is not None and row.customer_id is not None
    async with db.service_session(ServiceReason.ADMIN) as s:  # the call is linked to the caller's WhatsApp chat
        conv = (
            await s.execute(
                text("SELECT channel, thread_key, customer_id FROM conversations WHERE id = :c"),
                {"c": row.conversation_id},
            )
        ).one()
    assert (str(conv.channel), conv.thread_key, conv.customer_id) == ("whatsapp", "919833300001", row.customer_id)
    meta = bridges["wacid.A"].metadata
    assert meta["call_session_id"] == str(row.id) and meta["role"] == "customer" and meta["path"] == "webrtc"

    await c.handle_signal(connect(shop.wa, "wacid.A"))  # Meta retry / duplicate job
    assert len(graph.actions) == 2 and len(bridges) == 1
    await c.end("wacid.A", "test", terminate=False)


async def test_caller_hangup_ends_session(db: Database, graph: Graph, bridges: dict[str, FakeBridge]) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges)
    await c.handle_signal(connect(shop.wa, "wacid.B", "919833300002"))
    await c.handle_signal(terminate(shop.wa, "wacid.B"))
    row = await session_row(db, "wacid.B")
    assert (str(row.status), row.end_reason) == ("ended", "caller_hangup")
    assert bridges["wacid.B"].closed and "wacid.B" not in c.live
    assert ("terminate", "wacid.B") not in graph.actions  # Meta ended it; we do not terminate again


async def test_owner_call_is_marked_owner(db: Database, graph: Graph, bridges: dict[str, FakeBridge]) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges)
    await c.handle_signal(connect(shop.wa, "wacid.O", OWNER_PHONE[1:]))
    row = await session_row(db, "wacid.O")
    assert row.member_id == shop.owner_id and row.customer_id is None
    assert bridges["wacid.O"].metadata["role"] == "owner"
    await c.end("wacid.O", "test", terminate=False)


# ---------------------------------------------------------------- failures during setup


async def test_agent_timeout_terminates_and_fails(db: Database, graph: Graph, bridges: dict[str, FakeBridge]) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges, agent_joins=False)
    await c.handle_signal(connect(shop.wa, "wacid.T", "919833300003"))
    row = await session_row(db, "wacid.T")
    assert str(row.status) == "failed" and row.end_reason == "setup_failed:TimeoutError"
    assert graph.actions == [("pre_accept", "wacid.T"), ("terminate", "wacid.T")]  # never accepted
    assert bridges["wacid.T"].closed


async def test_hangup_during_setup_aborts_without_terminate(
    db: Database, graph: Graph, bridges: dict[str, FakeBridge]
) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges)
    original = c.bridge_factory

    def gated(call_id: str) -> Any:
        b = original(call_id)
        b.join_gate.clear()  # the agent is "still joining"
        return b

    c.bridge_factory = gated
    setup = asyncio.create_task(c.handle_signal(connect(shop.wa, "wacid.H", "919833300004")))
    await wait_until(lambda: _has(bridges, "wacid.H"))
    await asyncio.sleep(0.05)
    await c.handle_signal(terminate(shop.wa, "wacid.H"))
    await setup
    row = await session_row(db, "wacid.H")
    assert (str(row.status), row.end_reason) == ("ended", "caller_hangup_during_setup")
    assert ("accept", "wacid.H") not in graph.actions and ("terminate", "wacid.H") not in graph.actions
    assert bridges["wacid.H"].closed


async def _has(bridges: dict[str, FakeBridge], key: str) -> bool:
    return key in bridges


# ---------------------------------------------------------------- live call watchdog


async def test_agent_leaving_ends_call(db: Database, graph: Graph, bridges: dict[str, FakeBridge]) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges)
    await c.handle_signal(connect(shop.wa, "wacid.E", "919833300005"))
    bridges["wacid.E"].agent_here = False  # the agent said goodbye and left (end_call)

    async def ended() -> bool:
        return str((await session_row(db, "wacid.E")).status) == "ended"

    await wait_until(ended)
    row = await session_row(db, "wacid.E")
    assert row.end_reason == "agent_end" and ("terminate", "wacid.E") in graph.actions


async def test_caller_media_stop_ends_call(db: Database, graph: Graph, bridges: dict[str, FakeBridge]) -> None:
    shop = await make_shop(db)
    c = controller(graph, bridges)
    await c.handle_signal(connect(shop.wa, "wacid.M", "919833300006"))
    bridges["wacid.M"].caller_audio_ended.set()

    async def ended() -> bool:
        return str((await session_row(db, "wacid.M")).status) == "ended"

    await wait_until(ended)
    assert (await session_row(db, "wacid.M")).end_reason == "caller_media_ended"


# ---------------------------------------------------------------- restart recovery (done-when)


async def test_restart_recovery_marks_bridge_restart_and_terminates(
    db: Database, graph: Graph, bridges: dict[str, FakeBridge]
) -> None:
    shop = await make_shop(db)
    first = controller(graph, bridges, host="bridge-1")
    await first.handle_signal(connect(shop.wa, "wacid.R", "919833300007"))
    assert str((await session_row(db, "wacid.R")).status) == "active"
    # the process dies here: no cleanup runs. A new process with the same bridge id starts:
    graph.actions.clear()
    second = controller(graph, bridges, host="bridge-1")
    assert await second.recover() == 1
    row = await session_row(db, "wacid.R")
    assert (str(row.status), row.end_reason) == ("ended", "bridge_restart")
    assert graph.actions == [("terminate", "wacid.R")]
    assert await second.recover() == 0  # idempotent
    other = controller(graph, bridges, host="bridge-2")
    assert await other.recover() == 0  # never touches another bridge's calls


# ---------------------------------------------------------------- full path: webhook → inbox → job → bridge


def wa_call_webhook(pnid: str, call_id: str, wa: str) -> dict[str, Any]:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": pnid},
        "calls": [
            {
                "id": call_id,
                "from": wa,
                "to": "919800000000",
                "event": "connect",
                "timestamp": "1760000000",
                "session": {"sdp_type": "offer", "sdp": "v=0\r\noffer\r\n"},
            }
        ],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "calls", "value": value}]}],
    }


async def test_call_webhook_reaches_bridge_through_dedicated_queue(
    db: Database, graph: Graph, bridges: dict[str, FakeBridge]
) -> None:
    reg: Registry = reset_registry()
    pipeline_api.register(reg)
    gateway_api.register(reg)
    shop = await make_shop(db)
    await deliver(reg, db, "meta_whatsapp", wa_call_webhook(shop.pnid, "wacid.W", "919833300008"), shop.pnid)

    jobs = reg.require(JobQueuePort)  # type: ignore[type-abstract]
    # the pipeline worker never claims voice signals
    general = await jobs.claim("pipeline-worker", 30, 100_000, exclude=["voice.call_signal"])
    assert all(j.queue != "voice.call_signal" for j in general)
    for j in general:  # put them back so other tests are unaffected
        await jobs.fail(j, "pipeline-worker", "test")

    worker = BridgeWorker(controller(graph, bridges), jobs)
    [job] = [
        j
        for j in await jobs.claim(worker.worker_id, 60, 100, only=["voice.call_signal"])
        if j.payload["call"]["call_id"] == "wacid.W"
    ]
    await worker._run_job(job)
    row = await session_row(db, "wacid.W")
    assert str(row.status) == "active" and graph.actions[-1] == ("accept", "wacid.W")
    await worker.controller.end("wacid.W", "test", terminate=False)


# ---------------------------------------------------------------- voice agent session start


async def test_start_session_context(db: Database, graph: Graph, bridges: dict[str, FakeBridge]) -> None:
    shop = await make_shop(db)
    async with db.service_session(ServiceReason.SEED) as s:
        await s.execute(
            text("INSERT INTO business_profiles (business_id, profile) VALUES (:b, CAST(:p AS jsonb))"),
            {
                "b": shop.business_id,
                "p": json.dumps(
                    {
                        "identity": {"name": "Shop"},
                        "delivery": {"zones": [{"id": "z", "label": "Z", "fee_paise": 5000}]},
                    }
                ),
            },
        )
    c = controller(graph, bridges)
    await c.handle_signal(connect(shop.wa, "wacid.S", "919833300009"))
    row = await session_row(db, "wacid.S")
    ctx = await voice_api.start_session(row.id)
    assert ctx.role == "customer" and ctx.customer_id == row.customer_id
    assert "PHONE CALL" in ctx.instructions and "Business info" in ctx.instructions
    assert ctx.greeting.startswith("Namaste") and 5000 in ctx.allowed_paise
    await c.end("wacid.S", "test", terminate=False)
    with pytest.raises(voice_api.SessionNotLive):
        await voice_api.start_session(row.id)
