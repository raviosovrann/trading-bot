import { describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import { AuthProvider } from './useAuth'
import { BotEventsProvider, type BotSocket } from './useBotEvents'

class FakeSocket implements BotSocket {
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: ((ev?: { code?: number }) => void) | null = null
  close = vi.fn()
}

function setup() {
  const client = {
    getSession: vi.fn().mockResolvedValue({ username: 'op', roles: ['operator'] }),
  } as unknown as ApiClient
  const sockets: FakeSocket[] = []
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthProvider client={client}>
        <BotEventsProvider
          socketFactory={() => {
            const s = new FakeSocket()
            sockets.push(s)
            return s
          }}
        >
          <div>child</div>
        </BotEventsProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
  return { sockets }
}

describe('useBotEvents auth handling', () => {
  it('reconnects on a normal close but not after a 1008 auth close', async () => {
    const { sockets } = setup()
    // The session restore resolves (real timers); wait for the first socket.
    await waitFor(() => expect(sockets.length).toBe(1))

    vi.useFakeTimers()
    try {
      // A normal (non-auth) close schedules a reconnect.
      act(() => sockets[0].onclose?.({ code: 1006 }))
      act(() => vi.advanceTimersByTime(3000))
      expect(sockets.length).toBe(2)

      // A 1008 auth close does NOT reconnect.
      act(() => sockets[1].onclose?.({ code: 1008 }))
      act(() => vi.advanceTimersByTime(3000))
      expect(sockets.length).toBe(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
