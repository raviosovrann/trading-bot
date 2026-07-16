import { afterEach, describe, expect, it, vi } from 'vitest'
import { makeClient } from './client'

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

afterEach(() => vi.unstubAllGlobals())

describe('makeClient', () => {
  it('attaches the bearer token and hits the /api path', async () => {
    const fetchFn = mockFetch([])
    const client = makeClient(() => 'tok-123')
    await client.listBots()
    expect(fetchFn).toHaveBeenCalledOnce()
    const [url, init] = fetchFn.mock.calls[0]
    expect(url).toBe('/api/bots')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok-123')
  })

  it('omits the Authorization header when there is no token', async () => {
    const fetchFn = mockFetch({ token: 'x' })
    const client = makeClient(() => null)
    await client.login('u', 'p')
    const [url, init] = fetchFn.mock.calls[0]
    expect(url).toBe('/api/login')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('throws on a non-ok response', async () => {
    mockFetch('bad request', 400)
    const client = makeClient(() => 't')
    await expect(client.listBots()).rejects.toThrow(/400/)
  })

  it('returns undefined for a 204 response', async () => {
    const fetchFn = vi.fn().mockResolvedValue({ ok: true, status: 204, text: async () => '' })
    vi.stubGlobal('fetch', fetchFn)
    const client = makeClient(() => 't')
    await expect(client.putSecrets('coinbase', 'spot', { api_key: 'k' })).resolves.toBeUndefined()
  })
})
