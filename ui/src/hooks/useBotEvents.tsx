import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useAuth } from './useAuth'
import type { WsEvent } from '../types'

/** Minimal socket surface so tests can inject a fake. */
export interface BotSocket {
  onmessage: ((ev: { data: string }) => void) | null
  onclose: ((ev?: { code?: number }) => void) | null
  close: () => void
}

type Handler = (event: WsEvent) => void
type Subscribe = (handler: Handler) => () => void

const BotEventsContext = createContext<Subscribe | null>(null)

const RECONNECT_MS = 3000
// The server closes with 1008 (policy violation) when the session cookie is
// missing/expired. Reconnecting would just fail again, so we stop and let the
// central auth handler drop the user to /login.
const AUTH_CLOSE_CODE = 1008

function defaultSocketFactory(): BotSocket {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  // Authentication is the HttpOnly session cookie the browser sends on the
  // upgrade — no secret in the URL.
  const url = `${proto}://${window.location.host}/ws`
  // Native WebSocket satisfies BotSocket at runtime; its handler types are
  // wider (MessageEvent), hence the cast.
  return new WebSocket(url) as unknown as BotSocket
}

/**
 * Owns the single WS /ws connection and fans events out to subscribers.
 * Reconnects while authenticated; closes cleanly on logout/unmount.
 */
export function BotEventsProvider({
  children,
  socketFactory = defaultSocketFactory,
}: {
  children: ReactNode
  socketFactory?: () => BotSocket
}) {
  const { status, onUnauthorized } = useAuth()
  const handlers = useRef(new Set<Handler>())
  // Highest state `seq` seen per bot. State snapshots are authoritative, so a
  // frame that arrives after a newer one must be dropped rather than applied.
  const lastSeq = useRef(new Map<string, number>())

  useEffect(() => {
    if (status !== 'authed') return
    let disposed = false
    let socket: BotSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined

    function connect() {
      socket = socketFactory()
      socket.onmessage = (ev) => {
        let parsed: WsEvent
        try {
          parsed = JSON.parse(ev.data) as WsEvent
        } catch {
          return // ignore malformed frames
        }
        if (parsed.type === 'state') {
          const seen = lastSeq.current.get(parsed.bot_id)
          if (seen !== undefined && parsed.seq <= seen) return
          lastSeq.current.set(parsed.bot_id, parsed.seq)
        }
        handlers.current.forEach((h) => h(parsed))
      }
      socket.onclose = (ev) => {
        if (disposed) return
        // The server may have restarted with its counters back at zero, so the
        // old high-water marks would silently drop every new snapshot.
        lastSeq.current.clear()
        if (ev?.code === AUTH_CLOSE_CODE) {
          onUnauthorized() // session gone: stop retrying, drop to /login
          return
        }
        retryTimer = setTimeout(connect, RECONNECT_MS)
      }
    }

    connect()
    return () => {
      disposed = true
      clearTimeout(retryTimer)
      socket?.close()
    }
  }, [status, socketFactory, onUnauthorized])

  const subscribe = useMemo<Subscribe>(
    () => (handler) => {
      handlers.current.add(handler)
      return () => {
        handlers.current.delete(handler)
      }
    },
    [],
  )

  return <BotEventsContext.Provider value={subscribe}>{children}</BotEventsContext.Provider>
}

/** Subscribe to live bot events for the lifetime of the component. */
export function useBotEvents(handler: Handler): void {
  const subscribe = useContext(BotEventsContext)
  if (!subscribe) throw new Error('useBotEvents must be used within a BotEventsProvider')
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => subscribe((event) => ref.current(event)), [subscribe])
}
