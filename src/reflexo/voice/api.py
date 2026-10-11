"""Public interface of the `voice` module. Other modules import only from here (POC_01 §4).

voice-bridge (WhatsApp WebRTC to LiveKit), call sessions, in-call events, LiveKit voice agent. Spec: POC_04 §10, POC_01 §5.3.
"""

from reflexo.voice.context import SessionNotLive, VoiceCtx, start_session
from reflexo.voice.sessions import CallSession

__all__ = ["CallSession", "SessionNotLive", "VoiceCtx", "start_session"]

from reflexo.common.contracts import CallRouterPort
from reflexo.common.ports import Registry
from reflexo.voice.call_events import CallRouter

__all__ += ["CallRouter", "register"]


def register(registry: Registry) -> None:
    registry.provide(CallRouterPort, CallRouter())  # type: ignore[type-abstract]
