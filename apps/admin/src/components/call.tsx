// Calling the business from the browser: the same voice agent that answers WhatsApp calls, joined by microphone
// over LiveKit. The server creates the call (session, room, agent dispatch) and hands us a one-room token.
import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";
import { MessageCircle, Mic, MicOff, PhoneOff, Store } from "lucide-react";
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
            ? "Microphone permission was blocked. Allow it in the address bar and call again."
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

/** The in-call screen, laid over the customer's phone. Like WhatsApp, it can shrink to a bar so the chat stays
 * usable during the call - to send a photo or type an address the agent then talks about. */
export function CallScreen({ call, shop }: { call: ReturnType<typeof useWebCall>; shop: string }) {
  const [minimized, setMinimized] = useState(false);
  const done = call.state === "ended" || call.state === "failed";
  const { reset } = call;
  useEffect(() => {
    if (call.state === "idle") setMinimized(false);
    // a call that ends while the chat is open: back to the chat (the recap arrives there)
    else if (minimized && done && !call.error) reset();
  }, [call.state, call.error, minimized, done, reset]);
  if (call.state === "idle") return null;
  const last = call.captions.at(-1);
  if (minimized && !done)
    return (
      <div className="absolute inset-x-0 top-0 z-10 flex h-[56px] items-center gap-2 bg-[#1daa61] px-3 text-white shadow-md">
        <button onClick={() => setMinimized(false)} className="flex min-w-0 flex-1 items-center gap-2.5 text-left" title="Back to the call">
          <span className="relative flex h-2.5 w-2.5 shrink-0">
            <span className={cx("absolute inline-flex h-full w-full rounded-full bg-white opacity-75", call.speaking && "animate-ping")} />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-white" />
          </span>
          <span className="min-w-0">
            <span className="block text-[13px] font-semibold">
              On call · {call.state === "live" ? <Elapsed since={call.startedAt} /> : "connecting…"} · tap to return
            </span>
            <span className="block truncate text-[11.5px] text-white/85">
              {last ? `${last.who === "you" ? "You: " : ""}${last.text}` : "Send a photo or type here. The call hears about it"}
            </span>
          </span>
        </button>
        <button
          onClick={call.toggleMute}
          className={cx("flex h-9 w-9 shrink-0 items-center justify-center rounded-full", call.muted ? "bg-white text-[#1daa61]" : "bg-white/20")}
          title={call.muted ? "Unmute" : "Mute"}
        >
          {call.muted ? <MicOff size={16} /> : <Mic size={16} />}
        </button>
        <button onClick={call.hangUp} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#ea4335]" title="Hang up">
          <PhoneOff size={16} />
        </button>
      </div>
    );
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
          <div className="text-white/75">The call recap arrives in the chat, and anything promised is on the owner's phone.</div>
        ) : call.captions.length ? (
          call.captions.slice(-3).map((c) => (
            <div key={c.id} className={c.who === "shop" ? "text-white" : "text-white/55"}>
              {c.who === "you" ? "You: " : ""}
              {c.text}
            </div>
          ))
        ) : call.state === "live" ? (
          <div className="text-white/60">Speak naturally. Ask for a cake, a price, a delivery slot…</div>
        ) : (
          <div className="text-white/60">Connecting the AI employee…</div>
        )}
      </div>
      <div className="mt-auto flex items-end gap-7">
        {done ? (
          <button onClick={call.reset} className="rounded-full bg-white/15 px-6 py-3 text-[14px] font-semibold ring-1 ring-white/20 hover:bg-white/20">
            Back to chat
          </button>
        ) : (
          <>
            <CallButton label="Chat" onClick={() => setMinimized(true)} title="Open the chat during the call: send a photo or type">
              <MessageCircle size={22} />
            </CallButton>
            <CallButton
              label={call.muted ? "Unmute" : "Mute"}
              onClick={call.toggleMute}
              disabled={call.state !== "live" && call.state !== "ringing"}
              active={call.muted}
            >
              {call.muted ? <MicOff size={22} /> : <Mic size={22} />}
            </CallButton>
            <CallButton label="End" onClick={call.hangUp} danger>
              <PhoneOff size={24} />
            </CallButton>
          </>
        )}
      </div>
    </div>
  );
}

function CallButton({
  label,
  onClick,
  disabled,
  active,
  danger,
  title,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  danger?: boolean;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-1.5">
      <button
        onClick={onClick}
        disabled={disabled}
        title={title ?? label}
        className={cx(
          "flex items-center justify-center rounded-full transition disabled:opacity-40",
          danger ? "h-16 w-16 bg-[#ea4335] shadow-lg hover:bg-[#d33426]" : "h-14 w-14",
          !danger && (active ? "bg-white text-[#0b1418]" : "bg-white/15 hover:bg-white/20"),
        )}
      >
        {children}
      </button>
      <span className="text-[11px] text-white/70">{label}</span>
    </div>
  );
}
