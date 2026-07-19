// Types mirroring the FastAPI service (src/tradingbot/service/dto.py).
// Single source of truth for the API shape; keep in sync with the backend.

export interface Position {
  symbol: string
  side: 'long' | 'short' | 'flat'
  size: number
  entry_price: number
}

export interface BotView {
  id: string
  venue: string
  market_type: string
  strategy: string
  symbol: string
  timeframe: string
  quantity: number
  live: boolean
  per_bot_cap: number
  global_cap: number
  params: Record<string, unknown>
  status: string
  position: Position | null
  pnl: number
  last_decision: string | null
  // Running but starved of market data — orthogonal to `status` (#114).
  degraded?: boolean
  degraded_reason?: string | null
  // A venue limitation rather than a dropped connection: restarting cannot fix
  // it, so the UI must not suggest that (#170).
  degraded_permanent?: boolean
}

export interface Trade {
  bot_id: string
  action: string
  status: string
  ok: boolean
  order_id: string | null
  symbol: string | null
  ts: number | null
  // Stable per-bot cursor used to page backward through history (#122).
  seq: number | null
}

// Trade history is unbounded, so the API always returns one bounded page.
export interface TradePage {
  items: Trade[]
  next_cursor: number | null
}

export interface CreateBot {
  venue: string
  market_type: string
  strategy: string
  symbol: string
  timeframe: string
  quantity: number
  live: boolean
  per_bot_cap: number
  global_cap: number
  params: Record<string, unknown>
}

export interface PatchBot {
  live?: boolean
  per_bot_cap?: number
  global_cap?: number
  params?: Record<string, unknown>
}

export interface VenueOption {
  venue: string
  market_type: string
}

// Returned by POST /api/login and GET /api/session. The browser session lives
// in an HttpOnly cookie, so no secret is carried here.
export interface SessionInfo {
  username: string
  roles: string[]
}

// WebSocket /ws events: {type: "state"|"decision"|"order", bot_id, ...}

// The authoritative snapshot of a bot. Carries the whole view, so a client can
// apply it without refetching; `seq` increases per bot so a snapshot that
// arrives after a newer one can be discarded.
export interface BotStateEvent {
  type: 'state'
  bot_id: string
  seq: number
  status: string
  position: Position | null
  pnl: number
  last_decision: string | null
  degraded: boolean
  degraded_reason: string | null
  degraded_permanent: boolean
}

export interface DecisionEvent {
  type: 'decision'
  bot_id: string
  symbol: string
  ts: number
  text: string
}

export interface OrderEvent {
  type: 'order'
  bot_id: string
  action: string
  status: string
  ok: boolean
  order_id: string | null
}

// The server dropped events for this client: the live view is incomplete and
// must be resynchronized from the API rather than trusted (#122).
export interface OverflowEvent {
  type: 'overflow'
  dropped: number
}

export type WsEvent = BotStateEvent | DecisionEvent | OrderEvent | OverflowEvent
