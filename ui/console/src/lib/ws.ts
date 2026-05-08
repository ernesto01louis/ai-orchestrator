/**
 * /ws hook — single shared WebSocket per process, fan-out to subscribers.
 *
 * The FastAPI server broadcasts every log + status message to every
 * connected client (Phase 2.2 made this cross-instance via Redis
 * pub/sub). The hook lets each consumer optionally filter and bounds
 * the buffered-message count so a long-running tab can't grow without
 * limit.
 *
 * One socket is enough — the dev-server Vite proxy reuses the upgrade
 * onto the upstream FastAPI. Production same-origin builds connect
 * directly. Override the URL with VITE_ORCHESTRATOR_URL if needed.
 */

import { useEffect, useState } from "react"
import type { WsMessage } from "./types"

const MAX_BUFFER = 400

type Subscriber = (msg: WsMessage) => void

interface Bus {
  subscribe(fn: Subscriber): () => void
  state(): "connecting" | "open" | "closed"
}

function resolveWsUrl(): string {
  const fromEnv = import.meta.env.VITE_ORCHESTRATOR_URL
  if (fromEnv) {
    return fromEnv.replace(/^http/, "ws").replace(/\/$/, "") + "/ws"
  }
  // Same-origin / dev-proxy path — works for both `npm run dev`
  // (proxied to upstream) and the production build served by FastAPI.
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${proto}//${window.location.host}/ws`
}

const bus: Bus = (() => {
  const subs = new Set<Subscriber>()
  let socket: WebSocket | null = null
  let state: "connecting" | "open" | "closed" = "closed"
  let backoff = 1000

  function connect() {
    state = "connecting"
    try {
      socket = new WebSocket(resolveWsUrl())
    } catch {
      scheduleReconnect()
      return
    }
    socket.onopen = () => {
      state = "open"
      backoff = 1000
    }
    socket.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WsMessage
        subs.forEach((fn) => fn(msg))
      } catch {
        /* drop malformed frames silently */
      }
    }
    socket.onclose = () => {
      state = "closed"
      scheduleReconnect()
    }
    socket.onerror = () => {
      socket?.close()
    }
  }

  function scheduleReconnect() {
    if (subs.size === 0) return
    const wait = backoff
    backoff = Math.min(backoff * 2, 30_000)
    setTimeout(() => {
      if (subs.size > 0 && state !== "open") connect()
    }, wait)
  }

  return {
    subscribe(fn) {
      subs.add(fn)
      if (state === "closed") connect()
      return () => {
        subs.delete(fn)
        if (subs.size === 0 && socket) {
          socket.close()
          socket = null
          state = "closed"
        }
      }
    },
    state() {
      return state
    },
  }
})()

interface UseWsOptions {
  /** Optional predicate to drop messages before they enter the buffer. */
  filter?: (msg: WsMessage) => boolean
  /** Buffer size cap. Default 400. */
  bufferSize?: number
}

export function useWebSocket(options: UseWsOptions = {}) {
  const { filter, bufferSize = MAX_BUFFER } = options
  const [messages, setMessages] = useState<WsMessage[]>([])
  const [connected, setConnected] = useState(bus.state() === "open")

  useEffect(() => {
    const unsubscribe = bus.subscribe((msg) => {
      if (filter && !filter(msg)) return
      setMessages((prev) => {
        const next = prev.length >= bufferSize
          ? prev.slice(prev.length - bufferSize + 1)
          : prev.slice()
        next.push(msg)
        return next
      })
    })
    // Poll the bus state — cheap, simpler than another subscription API.
    const interval = setInterval(() => setConnected(bus.state() === "open"), 1000)
    return () => {
      unsubscribe()
      clearInterval(interval)
    }
  }, [filter, bufferSize])

  return { messages, connected }
}
