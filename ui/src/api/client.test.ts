import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { makeClient, UnauthorizedError } from './client'

function mockFetch(body: unknown, status = 200) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    json: async () => body,
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

beforeEach(() => {
  // Clear any cookies left by a previous test.
  document.cookie.split(';').forEach((c) => {
    document.cookie = c.replace(/=.*/, '=;expires=' + new Date(0).toUTCString() + ';path=/')
  })
})
afterEach(() => vi.unstubAllGlobals())

describe('makeClient', () => {
  it('sends same-origin credentials and no CSRF header on reads', async () => {
    const fetchFn = mockFetch([])
    await makeClient().listBots()
    const [url, init] = fetchFn.mock.calls[0]
    expect(url).toBe('/api/bots')
    expect(init.credentials).toBe('same-origin')
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBeUndefined()
  })

  it('echoes the tb_csrf cookie in the header on state-changing requests', async () => {
    document.cookie = 'tb_csrf=csrf-value;path=/'
    const fetchFn = mockFetch(undefined, 204)
    await makeClient().putSecrets('coinbase', 'spot', { api_key: 'k' })
    const [url, init] = fetchFn.mock.calls[0]
    expect(url).toBe('/api/venues/coinbase/spot/secrets')
    expect(init.method).toBe('PUT')
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-value')
  })

  it('posts credentials to /api/login without a bearer token', async () => {
    const fetchFn = mockFetch({ username: 'u', roles: ['operator'] })
    await makeClient().login('u', 'p')
    const [url, init] = fetchFn.mock.calls[0]
    expect(url).toBe('/api/login')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('throws UnauthorizedError on a 401', async () => {
    mockFetch('nope', 401)
    await expect(makeClient().listBots()).rejects.toBeInstanceOf(UnauthorizedError)
  })

  it('throws on a non-ok response', async () => {
    mockFetch('bad request', 400)
    await expect(makeClient().listBots()).rejects.toThrow(/400/)
  })

  it('returns undefined for a 204 response', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ ok: true, status: 204, text: async () => '' })
    vi.stubGlobal('fetch', fetchFn)
    await expect(
      makeClient().putSecrets('coinbase', 'spot', { api_key: 'k' }),
    ).resolves.toBeUndefined()
  })
})
