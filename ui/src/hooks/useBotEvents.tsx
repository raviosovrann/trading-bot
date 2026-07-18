import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useAuth } from './useAuth'
import type { WsEvent } from '../types'

/** Minimal socket surface so tests can inject a fake. */
export interface BotSocket {
  onmessage: ((ev: { data: string }) => void) | null
  onclose: (() => void) | null
  close: () => void
}

type Handler = (event: WsEvent) => void
type Subscribe = (handler: Handler) => () => void

const BotEventsContext = createContext<Subscribe | null>(null)

const RECONNECT_MS = 3000

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
  const { status } = useAuth()
  const handlers = useRef(new Set<Handler>())

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
        handlers.current.forEach((h) => h(parsed))
      }
      socket.onclose = () => {
        if (!disposed) retryTimer = setTimeout(connect, RECONNECT_MS)
      }
    }

    connect()
    return () => {
      disposed = true
      clearTimeout(retryTimer)
      socket?.close()
    }
  }, [status, socketFactory])

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
