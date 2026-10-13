// Calling the business from the browser: the same voice agent that answers WhatsApp calls, joined by microphone
// over LiveKit. The server creates the call (session, room, agent dispatch) and hands us a one-room token.
import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";
import { Mic, MicOff, PhoneOff, Store } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../lib/api";
import { cx } from "./ui";

export type CallState = "idle" | "starting" | "ringing" | "live" | "ended" | "failed";
type Caption = { id: string; who: "you" | "shop"; text: string };

export function useWebCall(businessId: string, actor: number, name?: string) {
  const [state, setState] = useState<CallState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [muted, setMuted] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [endedAfter, setEndedAfter] = useState<number | null>(null);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const room = useRef<Room | null>(null);
  const audio = useRef<HTMLMediaElement[]>([]);

  const cleanup = useCallback(() => {
    for (const el of audio.current) el.remove();
    audio.current = [];
    const r = room.current;
    room.current = null;
    if (r) void r.disconnect();
  }, []);

  const finish = useCallback(
    (failed?: string) => {
      setStartedAt((t) => {
        if (t) setEndedAfter(Math.round((Date.now() - t) / 1000));
        return null;
      });
      setSpeaking(false);
      if (failed) setError(failed);
      setState(failed ? "failed" : "ended");
      cleanup();
    },
    [cleanup],
  );

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("This browser can't use a microphone here (it needs HTTPS).");
      setState("failed");
      return;
    }
    setError(null);
    setCaptions([]);
    setEndedAfter(null);
    setMuted(false);
    setState("starting");
    try {
      const call = await api.post<{ url: string; token: string }>("/sim/call", { actor, name }, { business_id: businessId });
      const r = new Room({ audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      room.current = r;
      r.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind !== Track.Kind.Audio) return;
        const el = track.attach();
        el.style.display = "none";
        document.body.appendChild(el);
        audio.current.push(el);
        setState("live");
        setStartedAt((t) => t ?? Date.now());
      });
      r.on(RoomEvent.ActiveSpeakersChanged, (speakers) => setSpeaking(speakers.some((p) => p.identity !== r.localParticipant.identity)));
      // the agent leaving is the shop hanging up (it says goodbye first)
      r.on(RoomEvent.ParticipantDisconnected, () => finish());
      r.on(RoomEvent.Disconnected, () => {
        if (room.current === r) finish();
      });
      r.registerTextStreamHandler("lk.transcription", async (reader, from) => {
        const text = (await reader.readAll()).trim();
        if (!text) return;
        const who = from.identity === r.localParticipant.identity ? "you" : "shop";
        setCaptions((c) => [...c.slice(-5), { id: reader.info.id, who, text }]);
      });
      await r.connect(call.url, call.token);
      setState((s) => (s === "starting" ? "ringing" : s));
      await r.localParticipant.setMicrophoneEnabled(true);
      await r.startAudio();
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof DOMException && e.name === "NotAllowedError"
            ? "Microphone permission was blocked — allow it in the address bar and call again."
            : "The call could not connect. Please try again.";
      finish(msg);
    }
  }, [actor, businessId, finish, name]);

  const hangUp = useCallback(() => finish(), [finish]);
  const toggleMute = useCallback(() => {
    const r = room.current;
    if (!r) return;
    const next = !muted;
    void r.localParticipant.setMicrophoneEnabled(!next);
    setMuted(next);
  }, [muted]);
  const reset = useCallback(() => {
    setState("idle");
    setError(null);
  }, []);

  useEffect(() => cleanup, [cleanup]); // leaving the page hangs up
  return { state, error, muted, speaking, startedAt, endedAfter, captions, start, hangUp, toggleMute, reset };
}

function Elapsed({ since }: { since: number | null }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  const s = since ? Math.max(0, Math.round((Date.now() - since) / 1000)) : 0;
  return <span className="tabular">{`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`}</span>;
}

/** The in-call screen, laid over the customer's phone. */
export function CallScreen({ call, shop }: { call: ReturnType<typeof useWebCall>; shop: string }) {
  if (call.state === "idle") return null;
  const done = call.state === "ended" || call.state === "failed";
  const status =
    call.state === "starting"
      ? "Calling…"
      : call.state === "ringing"
        ? "Ringing…"
        : call.state === "live"
          ? null
          : call.state === "failed"
            ? "Call failed"
            : `Call ended${call.endedAfter ? ` · ${Math.floor(call.endedAfter / 60)}:${String(call.endedAfter % 60).padStart(2, "0")}` : ""}`;
  return (
    <div className="absolute inset-0 z-10 flex flex-col items-center bg-gradient-to-b from-[#0b3d35] via-[#0f2a2f] to-[#0b1418] px-6 pb-8 pt-14 text-white">
      <div className="text-[12px] uppercase tracking-[0.18em] text-white/60">WhatsApp voice call</div>
      <div className="mt-2 font-display text-[22px] font-semibold">{shop}</div>
      <div className="mt-1 text-[13px] text-white/70">{status ?? <Elapsed since={call.startedAt} />}</div>
      <div className="relative mt-10 flex h-32 w-32 items-center justify-center">
        <span
          className={cx(
            "absolute inset-0 rounded-full bg-[#25d366]/25 transition-transform duration-300",
            call.speaking ? "scale-125 animate-pulse" : call.state === "ringing" || call.state === "starting" ? "animate-ping" : "scale-100",
          )}
        />
        <span className="relative flex h-24 w-24 items-center justify-center rounded-full bg-white/10 ring-1 ring-white/20">
          <Store size={38} />
        </span>
      </div>
      <div className="mt-8 min-h-[96px] w-full space-y-1.5 text-center text-[13px] leading-snug">
        {call.error ? (
          <div className="text-[#ffb4a8]">{call.error}</div>
        ) : done ? (
          <div className="text-white/75">The call recap arrives in the chat — and anything promised is on the owner's phone.</div>
        ) : call.captions.length ? (
          call.captions.slice(-3).map((c) => (
            <div key={c.id} className={c.who === "shop" ? "text-white" : "text-white/55"}>
              {c.who === "you" ? "You: " : ""}
              {c.text}
            </div>
          ))
        ) : call.state === "live" ? (
          <div className="text-white/60">Speak in Hindi, English or Hinglish — ask for a cake, a price, a delivery slot…</div>
        ) : (
          <div className="text-white/60">Connecting the AI employee…</div>
        )}
      </div>
      <div className="mt-auto flex items-center gap-6">
        {done ? (
          <button onClick={call.reset} className="rounded-full bg-white/15 px-6 py-3 text-[14px] font-semibold ring-1 ring-white/20 hover:bg-white/20">
            Back to chat
          </button>
        ) : (
          <>
            <button
              onClick={call.toggleMute}
              disabled={call.state !== "live" && call.state !== "ringing"}
              className={cx("flex h-14 w-14 items-center justify-center rounded-full transition disabled:opacity-40", call.muted ? "bg-white text-[#0b1418]" : "bg-white/15")}
              title={call.muted ? "Unmute" : "Mute"}
            >
              {call.muted ? <MicOff size={22} /> : <Mic size={22} />}
            </button>
            <button onClick={call.hangUp} className="flex h-16 w-16 items-center justify-center rounded-full bg-[#ea4335] shadow-lg transition hover:bg-[#d33426]" title="Hang up">
              <PhoneOff size={26} />
            </button>
          </>
        )}
      </div>
    </div>
  );
}
