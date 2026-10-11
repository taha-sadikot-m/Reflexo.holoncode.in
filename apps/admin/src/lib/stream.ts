import { useEffect, useRef, useState } from "react";

import type { XrayEvent } from "./api";

export type StreamMode = "connecting" | "live" | "polling" | "offline";

/** Live X-ray events for a business over SSE (backfill first, then Redis relay or polling fallback). */
export function useXrayStream(businessId: string | undefined, max = 2000) {
  const [events, setEvents] = useState<XrayEvent[]>([]);
  const [mode, setMode] = useState<StreamMode>("connecting");
  const buffer = useRef<XrayEvent[]>([]);

  useEffect(() => {
    if (!businessId) return;
    setEvents([]);
    setMode("connecting");
    buffer.current = [];
    const source = new EventSource(`/admin/api/stream?business_id=${businessId}`, { withCredentials: true });
    let flushTimer: number | undefined;
    const flush = () => {
      flushTimer = undefined;
      const batch = buffer.current;
      buffer.current = [];
      if (batch.length) setEvents((prev) => [...prev, ...batch].slice(-max));
    };
    source.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.type === "mode") setMode(data.mode);
      else if (data.type === "xray") {
        buffer.current.push(data as XrayEvent);
        if (flushTimer === undefined) flushTimer = window.setTimeout(flush, 100); // batch renders
      }
    };
    source.onerror = () => setMode("offline"); // EventSource reconnects by itself
    return () => {
      source.close();
      if (flushTimer !== undefined) window.clearTimeout(flushTimer);
    };
  }, [businessId, max]);

  return { events, mode };
}
