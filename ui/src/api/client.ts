import type { BotView, CreateBot, PatchBot, Trade, VenueOption } from '../types'

// All server calls go through this one typed wrapper so components never fetch
// directly. It injects the bearer token and targets the same-origin /api prefix
// (the FastAPI service mounts its routes there; see 2B Task B5).
export function makeClient(getToken: () => string | null) {
  async function req<T>(path: string, init?: RequestInit): Promise<T> {
    const token = getToken()
    const res = await fetch(`/api${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.headers ?? {}),
      },
    })
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
    return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
  }

  return {
    login: (username: string, password: string) =>
      req<{ token: string }>('/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      }),
    listBots: () => req<BotView[]>('/bots'),
    getBot: (id: string) => req<BotView>(`/bots/${id}`),
    createBot: (bot: CreateBot) =>
      req<BotView>('/bots', { method: 'POST', body: JSON.stringify(bot) }),
    patchBot: (id: string, patch: PatchBot) =>
      req<BotView>(`/bots/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
    startBot: (id: string) => req<BotView>(`/bots/${id}/start`, { method: 'POST' }),
    stopBot: (id: string) => req<BotView>(`/bots/${id}/stop`, { method: 'POST' }),
    getTrades: (id: string) => req<Trade[]>(`/bots/${id}/trades`),
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
