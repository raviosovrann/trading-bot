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
}

export interface Trade {
  bot_id: string
  action: string
  status: string
  ok: boolean
  order_id: string | null
  symbol: string | null
  ts: number | null
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

// WebSocket /ws events: {type: "decision"|"order", bot_id, ...}
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

export type WsEvent = DecisionEvent | OrderEvent
