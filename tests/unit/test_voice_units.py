"""Voice units: WhatsApp SDP rules (H5), Meta calling retries/error mapping (H7), the sentence voice gate (§10.6)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from reflexo.voice import gate
from reflexo.voice.meta_calling import CallGraphError, MetaCalling
from reflexo.voice.sdp import VoiceSdpError, candidate_count, normalize_whatsapp_sdp, sdp_checks

ANSWER = (
    "\r\n".join(
        [
            "v=0",
            "o=- 1 2 IN IP4 0.0.0.0",
            "s=-",
            "t=0 0",
            "m=audio 9 UDP/TLS/RTP/SAVPF 111 101",
            "a=rtpmap:111 opus/48000/2",
            "a=rtpmap:101 telephone-event/8000",
            "a=ice-ufrag:abcd",
            "a=ice-pwd:efghijklmnopqrstuvwx",
            "a=fingerprint:sha-256 AA:BB:CC",
            "a=fingerprint:sha-384 DD:EE",
            "a=fingerprint:sha-512 FF:00",
            "a=setup:active",
            "a=candidate:1 1 udp 2130706431 192.168.1.20 50000 typ host",
            "a=candidate:2 1 udp 2130706431 169.254.3.4 50001 typ host",
            "a=candidate:3 1 udp 1694498815 203.0.113.9 50002 typ srflx raddr 192.168.1.20 rport 50000",
        ]
    )
    + "\r\n"
)


# ---------------------------------------------------------------- SDP


def test_normalize_keeps_one_sha256_and_drops_link_local() -> None:
    out = normalize_whatsapp_sdp(ANSWER)
    assert [ln for ln in out.splitlines() if ln.startswith("a=fingerprint")] == ["a=fingerprint:sha-256 AA:BB:CC"]
    assert "169.254.3.4" not in out
    assert candidate_count(out) == 2
    assert out.endswith("\r\n")


@pytest.mark.parametrize(
    ("mutation", "failed"),
    [
        (lambda s: s.replace("a=rtpmap:111 opus/48000/2", "a=rtpmap:0 PCMU/8000"), "Opus at 48 kHz"),
        (lambda s: s.replace("a=setup:active", "a=setup:passive"), "active DTLS role"),
        (lambda s: s.replace("a=ice-pwd:efghijklmnopqrstuvwx\r\n", ""), "ICE password"),
        (lambda s: s.replace("a=fingerprint:sha-256 AA:BB:CC\r\n", ""), "one SHA-256 fingerprint"),
    ],
)
def test_invalid_answers_rejected(mutation: Any, failed: str) -> None:
    with pytest.raises(VoiceSdpError, match=failed):
        normalize_whatsapp_sdp(mutation(ANSWER))


def test_offer_allows_actpass() -> None:
    offer = ANSWER.replace("a=setup:active", "a=setup:actpass")
    assert sdp_checks(normalize_whatsapp_sdp(offer, "offer"), "offer")["negotiable DTLS role"]


async def test_real_aiortc_answer_passes_whatsapp_rules() -> None:
    """An aiortc offer (stand-in for WhatsApp's) → our bridge's answer satisfies every WhatsApp rule."""
    from aiortc import RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    from reflexo.voice.bridge_webrtc import WhatsAppAudioBridge

    caller = RTCPeerConnection()
    caller.addTrack(AudioStreamTrack())
    await caller.setLocalDescription(await caller.createOffer())
    bridge = WhatsAppAudioBridge("test-call")
    try:
        answer = await bridge.create_sdp_answer(caller.localDescription.sdp)
        assert all(sdp_checks(answer, "answer").values())
    finally:
        await bridge.close()
        await caller.close()


# ---------------------------------------------------------------- Meta calling


def client(handler: Any) -> MetaCalling:
    return MetaCalling(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_pre_accept_body_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"success": True})

    await client(handler).pre_accept("PN1", "tok", "wacid.1", "v=0\r\n")
    body = json.loads(seen[0].content)
    assert seen[0].url.path.endswith("/PN1/calls") and seen[0].headers["authorization"] == "Bearer tok"
    assert body == {
        "messaging_product": "whatsapp",
        "call_id": "wacid.1",
        "action": "pre_accept",
        "session": {"sdp_type": "answer", "sdp": "v=0\r\n"},
    }


async def test_transient_errors_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reflexo.voice.meta_calling.RETRY_DELAYS_S", (0.0, 0.0))
    responses = iter([httpx.Response(503, json={}), httpx.Response(429, json={}), httpx.Response(200, json={})])
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return next(responses)

    await client(handler).accept("PN1", "tok", "wacid.1", "v=0")
    assert calls == 3


async def test_client_errors_not_retried() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"code": 138003, "message": "busy"}})

    with pytest.raises(CallGraphError) as err:
        await client(handler).terminate("PN1", "tok", "wacid.1")
    assert calls == 1 and err.value.code == 138003 and "already in progress" in str(err.value)


async def test_sdp_rejection_is_typed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 138008, "message": "bad sdp"}})

    with pytest.raises(VoiceSdpError):
        await client(handler).accept("PN1", "tok", "wacid.1", "v=0")


# ---------------------------------------------------------------- voice gate


def test_check_sentence() -> None:
    allowed = {95000, 5000}
    assert gate.check_sentence("Chocolate truffle 1 kg 950 rupees ka hai.", allowed)
    assert not gate.check_sentence("Aapke liye sirf 450 rupees.", allowed)
    assert gate.check_sentence("Delivery ka charge 50 rupees hai.", allowed)
    assert gate.check_sentence("Kal subah 10 baje tak ready ho jayega.", allowed)


class _Delta:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.content, self.tool_calls = content, tool_calls or []


class _Chunk:
    def __init__(self, delta: _Delta) -> None:
        self.delta = delta


async def _collect(chunks: list[Any], allowed: set[int]) -> list[Any]:
    async def stream() -> Any:
        for c in chunks:
            yield c

    return [c async for c in gate.gated(stream(), allowed, "hi-Latn")]


async def test_gate_replaces_wrong_price_sentence_only() -> None:
    out = await _collect(["1 kg ", "950 rupees ka hai. Aapke liye ", "450 rupees! ", "Aur kuch?"], {95000})
    text = "".join(c for c in out if isinstance(c, str))
    assert "950 rupees ka hai." in text
    assert "450" not in text and gate.safe_line("hi-Latn") in text
    assert text.rstrip().endswith("Aur kuch?")


async def test_gate_passes_tool_calls_and_flushes_text_first() -> None:
    tool = _Chunk(_Delta(tool_calls=["catalog_search"]))
    out = await _collect([_Chunk(_Delta("Ek second, ")), tool, "dekhti hoon."], set())
    assert out[0] == "Ek second, " and out[1] is tool and out[2] == "dekhti hoon."


# ---------------------------------------------------------------- outbound pacing (choppy-audio regression)


def test_paced_buffer_frames_and_prebuffer() -> None:
    from reflexo.voice.bridge_webrtc import FRAME_BYTES, AudioMetrics, PacedAudioBuffer

    m = AudioMetrics()
    buf = PacedAudioBuffer(m)
    ten_ms = b"\x01\x00" * 480  # LiveKit delivers 10 ms frames
    buf.push(ten_ms * 2)  # 20 ms buffered: below the 60 ms prebuffer -> still silence
    assert buf.pull() == bytes(FRAME_BYTES) and m.silence_frames_to_whatsapp == 1
    buf.push(ten_ms * 4)  # 60 ms buffered -> play
    out = [buf.pull() for _ in range(3)]
    assert all(len(f) == FRAME_BYTES and f != bytes(FRAME_BYTES) for f in out)
    assert m.frames_to_whatsapp == 3 and buf.buffered_ms == 0
    assert buf.pull() == bytes(FRAME_BYTES)  # drained -> silence, back to prebuffering


def test_paced_buffer_never_interleaves_silence_mid_utterance() -> None:
    from reflexo.voice.bridge_webrtc import AudioMetrics, PacedAudioBuffer

    m = AudioMetrics()
    buf = PacedAudioBuffer(m)
    ten_ms = b"\x02\x00" * 480
    buf.push(ten_ms * 6)
    for _ in range(50):  # 1 s of speech arriving in real time while we pull one 20 ms frame per tick
        buf.push(ten_ms * 2)
        buf.pull()
    assert m.silence_frames_to_whatsapp == 0 and m.frames_to_whatsapp == 50


def test_paced_buffer_caps_latency() -> None:
    from reflexo.voice.bridge_webrtc import AudioMetrics, PacedAudioBuffer

    m = AudioMetrics()
    buf = PacedAudioBuffer(m)
    buf.push(b"\x00\x01" * 48000 * 3)  # 3 s burst
    assert buf.buffered_ms == 1000 and m.dropped_to_whatsapp > 0


async def test_paced_track_runs_at_real_time() -> None:
    import time as _t

    from reflexo.voice.bridge_webrtc import AudioMetrics, _queued_track

    track = _queued_track(AudioMetrics())
    started = _t.monotonic()
    frames = [await track.recv() for _ in range(11)]
    elapsed = _t.monotonic() - started
    assert all(f.samples == 960 and f.sample_rate == 48000 for f in frames)
    assert [f.pts for f in frames[:3]] == [0, 960, 1920]
    assert 0.18 <= elapsed <= 0.4  # 10 intervals of 20 ms, not as-fast-as-possible
