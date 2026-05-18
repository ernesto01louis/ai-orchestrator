// /ws hook. When VITE_USE_MOCKS=1, subscribes to the fake bus in
// mocks.ts; otherwise opens a real WebSocket with auto-reconnect.

import { useEffect, useRef, useState } from "react";
import type { WsMessage } from "./types";
import { createMockWsBus } from "./mocks";

const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === "1";
const WS_URL =
  import.meta.env.VITE_WS_URL ??
  (typeof location !== "undefined"
    ? `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`
    : "ws://localhost:8000/ws");

const mockBus = USE_MOCKS ? createMockWsBus() : null;

interface Options {
  /** Optional client-side filter (e.g. by run_id). */
  filter?: (m: WsMessage) => boolean;
  /** Cap on buffered messages (default 400). */
  cap?: number;
}

export interface WsState {
  messages: WsMessage[];
  connected: boolean;
}

export function useWebSocket({ filter, cap = 400 }: Options = {}): WsState {
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const [connected, setConnected] = useState(USE_MOCKS);
  const filterRef = useRef(filter);
  filterRef.current = filter;

  useEffect(() => {
    if (mockBus) {
      const unsub = mockBus.subscribe((m) => {
        if (filterRef.current && !filterRef.current(m)) return;
        setMessages((prev) => {
          const next = prev.length > cap ? prev.slice(-cap) : prev.slice();
          next.push(m);
          return next;
        });
      });
      return unsub;
    }

    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const connect = () => {
      ws = new WebSocket(WS_URL);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) reconnectTimer = setTimeout(connect, 1500);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data) as WsMessage;
          if (filterRef.current && !filterRef.current(m)) return;
          setMessages((prev) => {
            const next = prev.length > cap ? prev.slice(-cap) : prev.slice();
            next.push(m);
            return next;
          });
        } catch {
          /* ignore malformed frames */
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [cap]);

  return { messages, connected };
}
