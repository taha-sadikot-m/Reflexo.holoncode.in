"""`voice-agent` process (POC_04 §10.1): `python -m reflexo.voice.agent_worker dev|start`.

A LiveKit Agents worker named `reflexo-voice`, dispatched explicitly by the voice-bridge for room `call-{id}`
with the call metadata. Pipeline: Silero VAD → Deepgram STT (streaming, multilingual) → Gemini (LiveKit Google
plugin; the one documented exception to the model gateway, for streaming latency) → Deepgram Aura TTS.
Settings follow the old repo's working worker (H11).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from reflexo.common.config import get_settings
from reflexo.common.enums import PipelineEventStatus
from reflexo.common.logging import get_logger
from reflexo.common.xray import emit

log = get_logger(__name__)


_bootstrapped = False


def _ensure_bootstrap() -> None:
    global _bootstrapped
    if not _bootstrapped:
        from reflexo.bootstrap import bootstrap

        bootstrap("reflexo-voice-agent")
        _bootstrapped = True


def prewarm(proc: Any) -> None:
    """Per idle job process, before any call: load Silero and bootstrap our modules, so a call starts on a warm
    process (measured: a cold first job adds ~10 s on Windows). `initialize_process_timeout` allows for it."""
    from livekit.plugins import silero

    proc.userdata["vad"] = silero.VAD.load()
    _ensure_bootstrap()


def _session(vad: Any) -> Any:
    from livekit.agents import AgentSession
    from livekit.plugins import deepgram, google

    s = get_settings()
    return AgentSession(
        vad=vad,
        stt=deepgram.STT(
            model=s.stt_model,
            language=s.stt_language,
            smart_format=True,
            filler_words=True,
            endpointing_ms=int(s.voice_endpointing_min_s * 1000),
            api_key=s.deepgram_api_key.get_secret_value(),
        ),
        llm=google.LLM(
            model=s.voice_model,
            api_key=s.google_api_key.get_secret_value(),
            temperature=0.3,
            max_output_tokens=300,
            thinking_config={"thinking_budget": 0},
        ),
        tts=deepgram.TTS(model=s.tts_model, api_key=s.deepgram_api_key.get_secret_value()),
        min_endpointing_delay=s.voice_endpointing_min_s,
        max_endpointing_delay=s.voice_endpointing_max_s,
        user_away_timeout=float(s.voice_silence_hangup_s),
        max_tool_steps=4,
    )


async def entrypoint(job: Any) -> None:
    from livekit.agents import AutoSubscribe

    from reflexo.voice import sessions
    from reflexo.voice.agent import GOODBYE, ReflexoVoiceAgent, record_turn
    from reflexo.voice.context import SessionNotLive, start_session

    _ensure_bootstrap()
    meta = json.loads(job.job.metadata or "{}")
    raw_id = meta.get("call_session_id")
    if not raw_id:
        log.error("voice_job_without_session", room=job.room.name)
        return
    call_session_id = UUID(str(raw_id))
    await job.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    try:
        ctx = await start_session(call_session_id)
    except SessionNotLive:
        log.warning("voice_session_not_live", call_session_id=str(call_session_id))
        job.shutdown("session_not_live")
        return

    session = _session(job.proc.userdata.get("vad"))
    tasks: set[asyncio.Task[None]] = set()

    def spawn(coro: Any) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    @session.on("conversation_item_added")  # type: ignore[untyped-decorator]
    def on_item(ev: Any) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = str(getattr(item, "text_content", "") or "").strip()
        if role in ("user", "assistant") and text:
            spawn(record_turn(ctx, "caller" if role == "user" else "agent", text))
            if role == "assistant":
                spawn(
                    emit(
                        "voice_turn",
                        PipelineEventStatus.OK,
                        business_id=ctx.business_id,
                        call_session_id=str(ctx.call_session_id),
                        chars=len(text),
                    )
                )

    @session.on("user_state_changed")  # type: ignore[untyped-decorator]
    def on_user_state(ev: Any) -> None:
        if getattr(ev, "new_state", None) == "away":  # silence for voice_silence_hangup_s

            async def goodbye() -> None:
                await session.say(GOODBYE.get(ctx.language, GOODBYE["hi-Latn"]), allow_interruptions=False)
                job.shutdown("silence")

            spawn(goodbye())

    async def on_shutdown(reason: str = "") -> None:
        if tasks:
            await asyncio.wait(tasks, timeout=3)
        # our own shutdowns carry a reason ("agent_end:done", "silence"); anything else means the room went away
        why = str(reason or "")
        why = why if why.startswith(("agent_end", "silence", "session_not_live")) else "caller_hangup"
        ended = await sessions.mark_ended(call_session_id, why[:100])
        await emit(
            "voice_turn",
            PipelineEventStatus.OK,
            business_id=ctx.business_id,
            call_session_id=str(call_session_id),
            step="shutdown",
            reason=str(reason),
            ended_here=ended,
        )

    job.add_shutdown_callback(on_shutdown)
    await session.start(agent=ReflexoVoiceAgent(ctx), room=job.room)


def main() -> None:
    from livekit.agents import JobExecutorType, WorkerOptions, cli

    s = get_settings()
    missing = [
        n
        for n, v in (
            ("REFLEXO_LIVEKIT_URL", s.livekit_url),
            ("REFLEXO_LIVEKIT_API_KEY", s.livekit_api_key),
            ("REFLEXO_LIVEKIT_API_SECRET", s.livekit_api_secret.get_secret_value()),
            ("REFLEXO_DEEPGRAM_API_KEY", s.deepgram_api_key.get_secret_value()),
            ("REFLEXO_GOOGLE_API_KEY", s.google_api_key.get_secret_value()),
        )
        if not v
    ]
    if missing:
        raise SystemExit(f"voice agent not configured: {', '.join(missing)}")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=s.voice_agent_name,  # explicit dispatch only (the bridge requests it per call)
            ws_url=s.livekit_url,
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret.get_secret_value(),
            job_executor_type=JobExecutorType.PROCESS,  # asyncpg/redis clients are loop-bound: one process per call
            num_idle_processes=2,
            initialize_process_timeout=60.0,
        )
    )


if __name__ == "__main__":
    main()
