"""WhatsApp WebRTC (aiortc) ↔ LiveKit room audio bridge (harvested H5 from the old `voice/bridge.py`).

Kept: SDP handling, Opus-only peer connection, the 48 kHz inbound pump. Replaced: the outbound frame queue with a
paced PCM buffer (`PacedAudioBuffer`), which fixed choppy agent audio on real calls. Added: explicit agent dispatch,
explicit timeouts, audio metrics for tracing, and an idempotent `close()` that always releases both legs and
deletes the room.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import numpy as np

from reflexo.common.logging import get_logger
from reflexo.voice.sdp import ALLOWED_MIME_TYPES, VoiceSdpError, candidate_count, normalize_whatsapp_sdp

log = get_logger(__name__)
SAMPLE_RATE = 48000
FRAME_SAMPLES = SAMPLE_RATE // 50  # 20 ms
ICE_TIMEOUT_S = 5.0
CALLER_IDENTITY = "whatsapp-caller"


@dataclass
class AudioMetrics:
    frames_from_whatsapp: int = 0
    frames_to_whatsapp: int = 0
    silence_frames_to_whatsapp: int = 0
    dropped_to_whatsapp: int = 0
    first_audio_at: float | None = None
    agent_audio_at: float | None = None
    started_at: float = field(default_factory=time.monotonic)

    def as_attrs(self) -> dict[str, Any]:
        return {
            "frames_in": self.frames_from_whatsapp,
            "frames_out": self.frames_to_whatsapp,
            "silence_out": self.silence_frames_to_whatsapp,
            "dropped_out": self.dropped_to_whatsapp,
            "first_audio_ms": int((self.first_audio_at - self.started_at) * 1000) if self.first_audio_at else None,
            "agent_audio_ms": int((self.agent_audio_at - self.started_at) * 1000) if self.agent_audio_at else None,
        }


FRAME_BYTES = FRAME_SAMPLES * 2  # s16 mono
PREBUFFER_SAMPLES = SAMPLE_RATE * 60 // 1000  # absorb network jitter at the start of each utterance
MAX_BUFFER_SAMPLES = SAMPLE_RATE  # 1 s; beyond that drop the oldest audio (latency beats completeness)


class PacedAudioBuffer:
    """PCM buffer between LiveKit (frames of any size, arriving in bursts) and the WhatsApp RTP sender, which
    pulls exactly one 20 ms frame per tick. Silence is produced only when nothing is buffered.

    The first bridge version queued LiveKit frames and inserted a 20 ms silence frame whenever the queue was
    momentarily empty, without pacing; aiortc pulled as fast as it could, so speech went out chopped with gaps
    (measured on a real call: 7,573 audio + 4,424 silence frames in 76 s instead of ~3,800)."""

    def __init__(self, metrics: AudioMetrics) -> None:
        self.metrics = metrics
        self._buf = bytearray()
        self._playing = False

    def push(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        excess = len(self._buf) - MAX_BUFFER_SAMPLES * 2
        if excess > 0:
            excess += excess % 2
            del self._buf[:excess]
            self.metrics.dropped_to_whatsapp += max(1, excess // FRAME_BYTES)

    def pull(self) -> bytes:
        """Exactly FRAME_BYTES of s16 mono PCM."""
        if not self._playing and len(self._buf) >= PREBUFFER_SAMPLES * 2:
            self._playing = True
        if self._playing and self._buf:
            chunk = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:  # end of an utterance: pad the tail, then prebuffer the next one
                chunk += bytes(FRAME_BYTES - len(chunk))
                self._playing = False
            self.metrics.frames_to_whatsapp += 1
            return chunk
        self._playing = False
        self.metrics.silence_frames_to_whatsapp += 1
        return bytes(FRAME_BYTES)

    @property
    def buffered_ms(self) -> int:
        return len(self._buf) // 2 * 1000 // SAMPLE_RATE


def _queued_track(metrics: AudioMetrics) -> Any:
    """Outbound aiortc track fed by LiveKit agent audio, paced at real time (created lazily: tests need no aiortc)."""
    from aiortc.mediastreams import AudioStreamTrack
    from av import AudioFrame

    class PacedAudioTrack(AudioStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.buffer = PacedAudioBuffer(metrics)
            self._t0: float | None = None
            self._pts = 0

        def enqueue(self, lk_frame: Any) -> None:
            self.buffer.push(bytes(lk_frame.data))

        async def recv(self) -> Any:
            # pace like aiortc's own tracks: one 20 ms frame per 20 ms of wall clock
            if self._t0 is None:
                self._t0 = time.monotonic()
            else:
                wait = self._t0 + self._pts / SAMPLE_RATE - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            pcm = np.frombuffer(self.buffer.pull(), dtype=np.int16).reshape(1, -1)
            frame = AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.sample_rate = SAMPLE_RATE
            frame.pts = self._pts
            frame.time_base = Fraction(1, SAMPLE_RATE)
            self._pts += FRAME_SAMPLES
            return frame

    return PacedAudioTrack()


async def _wait_ice_complete(pc: Any, limit_s: float = ICE_TIMEOUT_S) -> None:
    if getattr(pc, "iceGatheringState", None) == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")  # type: ignore[untyped-decorator]
    def _on_ice() -> None:
        if getattr(pc, "iceGatheringState", None) == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=limit_s)
    except TimeoutError:
        log.warning("bridge_ice_gathering_timeout_using_current_sdp")


class WhatsAppAudioBridge:
    """One live call: WhatsApp peer connection on one side, LiveKit room `call-{call_id}` on the other."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.room_name = f"call-{call_id}"
        self.metrics = AudioMetrics()
        self.agent_audio_ready = asyncio.Event()
        self.caller_audio_ended = asyncio.Event()
        self._pc: Any = None
        self._room: Any = None
        self._source: Any = None
        self._lk_api: Any = None
        self._wa_track: Any = None
        self._out_track: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._wa_forward: asyncio.Task[None] | None = None
        self._closed = False

    # ---------------------------------------------------------------- WhatsApp leg
    def _open_peer_connection(self) -> None:
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCRtpSender

        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]))
        self._out_track = _queued_track(self.metrics)
        self._pc.addTrack(self._out_track)
        try:
            caps = RTCRtpSender.getCapabilities("audio")
            preferred = [c for c in caps.codecs if c.mimeType.lower() in ALLOWED_MIME_TYPES]
            for transceiver in self._pc.getTransceivers():
                if transceiver.kind == "audio" and preferred:
                    transceiver.setCodecPreferences(preferred)
        except Exception:  # a wider codec list still negotiates; Meta just prefers Opus only
            log.debug("bridge_codec_preference_skipped", call_id=self.call_id)

        @self._pc.on("track")  # type: ignore[untyped-decorator]
        def on_track(track: Any) -> None:
            if track.kind == "audio":
                self._wa_track = track
                self._maybe_start_whatsapp_forwarding()

        @self._pc.on("connectionstatechange")  # type: ignore[untyped-decorator]
        def on_state() -> None:
            state = getattr(self._pc, "connectionState", None) if self._pc else None
            log.info("bridge_peer_state", call_id=self.call_id, state=state)
            if state in ("failed", "closed"):
                self.caller_audio_ended.set()

    async def create_sdp_answer(self, sdp_offer: str) -> str:
        if not sdp_offer or not sdp_offer.strip():
            raise VoiceSdpError("empty SDP offer")
        from aiortc import RTCSessionDescription

        self._open_peer_connection()
        try:
            await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp_offer, type="offer"))
            await self._pc.setLocalDescription(await self._pc.createAnswer())
            await _wait_ice_complete(self._pc)
        except VoiceSdpError:
            raise
        except Exception as exc:
            raise VoiceSdpError(f"{type(exc).__name__}: {exc}") from exc
        sdp = getattr(self._pc.localDescription, "sdp", None)
        if not sdp:
            raise VoiceSdpError("empty SDP answer")
        normalized = normalize_whatsapp_sdp(sdp)
        log.info("bridge_sdp_answer_ready", call_id=self.call_id, candidates=candidate_count(normalized))
        return normalized

    # ---------------------------------------------------------------- LiveKit leg
    async def connect_livekit(
        self, url: str, api_key: str, api_secret: str, *, agent_name: str, metadata: dict[str, Any]
    ) -> None:
        """Create the room, explicitly dispatch the voice agent with the call metadata, and publish caller audio."""
        from livekit import api as lk_api
        from livekit import rtc

        meta = json.dumps(metadata)
        self._lk_api = lk_api.LiveKitAPI(
            url.replace("wss://", "https://").replace("ws://", "http://"), api_key, api_secret
        )
        try:
            await self._lk_api.room.create_room(
                lk_api.CreateRoomRequest(name=self.room_name, empty_timeout=120, max_participants=3, metadata=meta)
            )
        except Exception as exc:  # already exists (Meta retry) is fine; the dispatch below is what matters
            log.info("bridge_room_create_skipped", call_id=self.call_id, error=type(exc).__name__)
        await self._lk_api.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name=agent_name, room=self.room_name, metadata=meta)
        )
        token = (
            lk_api.AccessToken(api_key, api_secret)
            .with_identity(CALLER_IDENTITY)
            .with_name("WhatsApp caller")
            .with_grants(lk_api.VideoGrants(room_join=True, room=self.room_name, can_publish=True, can_subscribe=True))
            .with_metadata(meta)
            .to_jwt()
        )
        self._room = rtc.Room()

        @self._room.on("track_subscribed")  # type: ignore[untyped-decorator]
        def on_track_subscribed(track: Any, publication: Any, participant: Any) -> None:
            if getattr(track, "kind", None) == rtc.TrackKind.KIND_AUDIO:
                if self.metrics.agent_audio_at is None:
                    self.metrics.agent_audio_at = time.monotonic()
                self.agent_audio_ready.set()
                self._tasks.append(asyncio.create_task(self._forward_livekit_to_whatsapp(track)))

        @self._room.on("participant_disconnected")  # type: ignore[untyped-decorator]
        def on_left(participant: Any) -> None:
            log.info("bridge_participant_left", call_id=self.call_id, identity=getattr(participant, "identity", ""))

        await self._room.connect(url, token)
        self._source = rtc.AudioSource(sample_rate=SAMPLE_RATE, num_channels=1)
        track = rtc.LocalAudioTrack.create_audio_track("whatsapp-audio", self._source)
        await self._room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        self._maybe_start_whatsapp_forwarding()
        log.info("bridge_livekit_connected", call_id=self.call_id, room=self.room_name)

    async def wait_for_agent(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self.agent_audio_ready.wait(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False

    async def agent_present(self) -> bool:
        """Is a non-caller participant still in the room? (the agent leaves after `end_call`)"""
        if self._room is None:
            return False
        return any(p.identity != CALLER_IDENTITY for p in self._room.remote_participants.values())

    # ---------------------------------------------------------------- audio pump
    def _maybe_start_whatsapp_forwarding(self) -> None:
        if self._closed or self._wa_track is None or self._source is None:
            return
        if self._wa_forward is not None and not self._wa_forward.done():
            return
        self._wa_forward = asyncio.create_task(self._forward_whatsapp_to_livekit())
        self._tasks.append(self._wa_forward)

    async def _forward_whatsapp_to_livekit(self) -> None:
        from aiortc.mediastreams import MediaStreamError
        from av import AudioResampler
        from livekit import rtc

        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        try:
            while not self._closed:
                frame = await self._wa_track.recv()
                if self.metrics.first_audio_at is None:
                    self.metrics.first_audio_at = time.monotonic()
                for mono in resampler.resample(frame):
                    samples = int(mono.samples)
                    data = bytes(mono.planes[0])[: samples * 2]
                    await self._source.capture_frame(
                        rtc.AudioFrame(data=data, sample_rate=SAMPLE_RATE, num_channels=1, samples_per_channel=samples)
                    )
                    self.metrics.frames_from_whatsapp += 1
        except MediaStreamError:
            log.info("bridge_caller_audio_ended", call_id=self.call_id)
        except Exception:
            if not self._closed:
                log.exception("bridge_whatsapp_to_livekit_failed", call_id=self.call_id)
        finally:
            self.caller_audio_ended.set()

    async def _forward_livekit_to_whatsapp(self, lk_track: Any) -> None:
        from livekit import rtc

        stream = rtc.AudioStream(lk_track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            async for event in stream:
                if self._closed:
                    break
                if self._out_track is not None:
                    self._out_track.enqueue(getattr(event, "frame", event))
        except Exception:
            if not self._closed:
                log.exception("bridge_livekit_to_whatsapp_failed", call_id=self.call_id)
        finally:
            await stream.aclose()

    # ---------------------------------------------------------------- teardown
    async def close(self) -> None:
        """Idempotent: cancel pumps, leave and delete the room, close the peer connection."""
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        steps: list[tuple[str, Any]] = []
        if self._room is not None:
            steps.append(("room_disconnect", self._room.disconnect()))
        if self._lk_api is not None:
            from livekit import api as lk_api

            steps.append(("room_delete", self._lk_api.room.delete_room(lk_api.DeleteRoomRequest(room=self.room_name))))
        if self._pc is not None:
            steps.append(("peer_close", self._pc.close()))
        for name, coro in steps:
            try:
                await asyncio.wait_for(coro, timeout=5.0)
            except Exception as exc:  # cleanup must continue whatever fails
                log.info("bridge_close_step_failed", call_id=self.call_id, step=name, error=type(exc).__name__)
        if self._lk_api is not None:
            try:
                await self._lk_api.aclose()
            except Exception:  # noqa: S110 - best effort
                pass
        self._room = self._lk_api = self._pc = None
        log.info("bridge_closed", call_id=self.call_id, **self.metrics.as_attrs())
