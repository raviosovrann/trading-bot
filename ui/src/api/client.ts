import type { BotView, CreateBot, PatchBot, SessionInfo, TradePage, VenueOption } from '../types'

/** Thrown on a 401 so callers can centrally clear auth and redirect to login. */
/**
 * Pull the human-readable message out of an API error body.
 *
 * FastAPI puts the explanation in `detail`; rendering the encoded envelope
 * instead buries the one sentence the operator actually needs.
 */
function describeError(body: string): string {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown }
    if (typeof parsed.detail === 'string' && parsed.detail) return parsed.detail
  } catch {
    // Not JSON — fall through and show whatever the server sent.
  }
  return body
}

export class UnauthorizedError extends Error {
  constructor() {
    super('Unauthorized')
    this.name = 'UnauthorizedError'
  }
}

/** Read a readable (non-HttpOnly) cookie value, or null when absent. */
function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'))
  return match ? decodeURIComponent(match[1]) : null
}

// All server calls go through this one typed wrapper so components never fetch
// directly. Authentication is the HttpOnly session cookie sent automatically
// with `credentials: same-origin`; state-changing requests echo the readable
// `tb_csrf` cookie back in the X-CSRF-Token header (double-submit CSRF).
//
// `onUnauthorized` centralizes 401 handling: a single hook (clear auth, close
// the socket, clear caches, redirect to login) fires wherever a session lapses,
// so no component has to handle a rotated/expired session on its own.
export function makeClient(onUnauthorized?: () => void) {
  async function req<T>(path: string, init?: RequestInit): Promise<T> {
    const method = (init?.method ?? 'GET').toUpperCase()
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...((init?.headers as Record<string, string>) ?? {}),
    }
    if (method !== 'GET' && method !== 'HEAD') {
      const csrf = readCookie('tb_csrf')
      if (csrf) headers['X-CSRF-Token'] = csrf
    }
    const res = await fetch(`/api${path}`, { ...init, credentials: 'same-origin', headers })
    if (res.status === 401) {
      // The session-restore probe expects 401 for a logged-out visitor, so
      // suppress the global handler for that one call.
      if (path !== '/session') onUnauthorized?.()
      throw new UnauthorizedError()
    }
    if (!res.ok) throw new Error(`${res.status} ${describeError(await res.text())}`)
    return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
  }

  return {
    login: (username: string, password: string) =>
      req<SessionInfo>('/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      }),
    logout: () => req<void>('/logout', { method: 'POST' }),
    getSession: () => req<SessionInfo>('/session'),
    listBots: () => req<BotView[]>('/bots'),
    getBot: (id: string) => req<BotView>(`/bots/${id}`),
    createBot: (bot: CreateBot) =>
      req<BotView>('/bots', { method: 'POST', body: JSON.stringify(bot) }),
    patchBot: (id: string, patch: PatchBot) =>
      req<BotView>(`/bots/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
    startBot: (id: string) => req<BotView>(`/bots/${id}/start`, { method: 'POST' }),
    stopBot: (id: string) => req<BotView>(`/bots/${id}/stop`, { method: 'POST' }),
    getTrades: (id: string, opts: { limit?: number; before?: number | null } = {}) => {
      const params = new URLSearchParams({ limit: String(opts.limit ?? 50) })
      if (opts.before != null) params.set('before', String(opts.before))
      return req<TradePage>(`/bots/${id}/trades?${params.toString()}`)
    },
    listVenues: () => req<VenueOption[]>('/venues'),
    listStrategies: () => req<string[]>('/strategies'),
    putSecrets: (venue: string, marketType: string, creds: Record<string, string>) =>
      req<void>(`/venues/${venue}/${marketType}/secrets`, {
        method: 'PUT',
        body: JSON.stringify(creds),
      }),
  }
}

export type ApiClient = ReturnType<typeof makeClient>
