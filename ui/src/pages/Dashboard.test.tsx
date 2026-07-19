import { describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { BotView, WsEvent } from '../types'
import { AuthProvider } from '../hooks/useAuth'
import { BotEventsProvider, type BotSocket } from '../hooks/useBotEvents'
import { Dashboard } from './Dashboard'

function bot(overrides: Partial<BotView> = {}): BotView {
  return {
    id: '1',
    venue: 'coinbase',
    market_type: 'spot',
    strategy: 'example',
    symbol: 'BTC/USD',
    timeframe: '1m',
    quantity: 0.1,
    live: false,
    per_bot_cap: 1000,
    global_cap: 10000,
    params: {},
    status: 'created',
    position: null,
    pnl: 0,
    last_decision: null,
    ...overrides,
  }
}

class FakeSocket implements BotSocket {
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: ((ev?: { code?: number }) => void) | null = null
  close = vi.fn()
}

function setup(bots: BotView[]) {
  const client = {
    getSession: vi.fn().mockResolvedValue({ username: 'op', roles: ['operator'] }),
    listBots: vi.fn().mockResolvedValue(bots),
    startBot: vi.fn().mockResolvedValue(bots[0]),
    stopBot: vi.fn().mockResolvedValue(bots[0]),
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
          <MemoryRouter>
            <Dashboard />
          </MemoryRouter>
        </BotEventsProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
  const emit = (event: WsEvent) =>
    act(() => sockets.forEach((s) => s.onmessage?.({ data: JSON.stringify(event) })))
  // Drop the socket the way a server restart would, so the provider reconnects.
  const reconnect = () => act(() => sockets.forEach((s) => s.onclose?.({ code: 1006 })))
  return { client, emit, reconnect }
}

describe('Dashboard', () => {
  it('renders bots and updates a row on a ws decision event', async () => {
    const { emit } = setup([bot({ id: '1', symbol: 'DOGE/USD' })])
    expect(await screen.findByText('DOGE/USD')).toBeInTheDocument()
    emit({ type: 'decision', bot_id: '1', symbol: 'DOGE/USD', ts: 1, text: 'BUY signal' })
    expect(await screen.findByText(/BUY signal/)).toBeInTheDocument()
  })

  it('applies a ws state event to the row without refetching', async () => {
    const { client, emit } = setup([bot({ id: '1', symbol: 'DOGE/USD', status: 'created' })])
    expect(await screen.findByText('DOGE/USD')).toBeInTheDocument()
    const fetchesBefore = (client.listBots as unknown as { mock: { calls: unknown[] } }).mock.calls
      .length

    emit({
      type: 'state',
      bot_id: '1',
      seq: 1,
      status: 'running',
      position: { symbol: 'DOGE/USD', side: 'long', size: 2, entry_price: 10 },
      pnl: 4.5,
      last_decision: 'BUY signal',
      degraded: false,
      degraded_reason: null,
    })

    expect(await screen.findByText('running')).toBeInTheDocument()
    expect(screen.getByText('4.50')).toBeInTheDocument()
    expect(screen.getByText(/long 2 @ 10/)).toBeInTheDocument()
    expect((client.listBots as unknown as { mock: { calls: unknown[] } }).mock.calls.length).toBe(
      fetchesBefore,
    )
  })

  it('shows a runtime failure arriving over the socket', async () => {
    const { emit } = setup([bot({ id: '1', status: 'running' })])
    expect(await screen.findByText('running')).toBeInTheDocument()
    emit({
      type: 'state',
      bot_id: '1',
      seq: 2,
      status: 'failed',
      position: null,
      pnl: 0,
      last_decision: null,
      degraded: false,
      degraded_reason: null,
    })
    expect(await screen.findByText('failed')).toBeInTheDocument()
  })

  it('flags a degraded bot without changing its status', async () => {
    const { emit } = setup([bot({ id: '1', status: 'running' })])
    expect(await screen.findByText('running')).toBeInTheDocument()
    emit({
      type: 'state',
      bot_id: '1',
      seq: 3,
      status: 'running',
      position: null,
      pnl: 0,
      last_decision: null,
      degraded: true,
      degraded_reason: 'stream ended without an unsubscribe',
    })
    expect(await screen.findByText(/no data/i)).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('ignores a state event that arrives out of order', async () => {
    const { emit } = setup([bot({ id: '1', status: 'created' })])
    expect(await screen.findByText('created')).toBeInTheDocument()
    const snapshot = {
      type: 'state' as const,
      bot_id: '1',
      position: null,
      pnl: 0,
      last_decision: null,
      degraded: false,
      degraded_reason: null,
    }
    emit({ ...snapshot, seq: 5, status: 'running' })
    expect(await screen.findByText('running')).toBeInTheDocument()
    // A stale frame must not resurrect the older status.
    emit({ ...snapshot, seq: 4, status: 'starting' })
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('accepts a restarted server sequence after a reconnect', async () => {
    const { emit, reconnect } = setup([bot({ id: '1', status: 'created' })])
    expect(await screen.findByText('created')).toBeInTheDocument()
    const snapshot = {
      type: 'state' as const,
      bot_id: '1',
      position: null,
      pnl: 0,
      last_decision: null,
      degraded: false,
      degraded_reason: null,
    }
    emit({ ...snapshot, seq: 9, status: 'running' })
    expect(await screen.findByText('running')).toBeInTheDocument()

    // The backend restarted: its counter is back at 1 and must not be dropped.
    reconnect()
    emit({ ...snapshot, seq: 1, status: 'stopped' })
    expect(await screen.findByText('stopped')).toBeInTheDocument()
  })

  it('refetches the table when the server reports dropped events', async () => {
    const { client, emit } = setup([bot({ id: '1', status: 'running' })])
    expect(await screen.findByText('running')).toBeInTheDocument()
    const before = (client.listBots as unknown as { mock: { calls: unknown[] } }).mock.calls.length

    emit({ type: 'overflow', dropped: 9 })

    await waitFor(() =>
      expect(
        (client.listBots as unknown as { mock: { calls: unknown[] } }).mock.calls.length,
      ).toBeGreaterThan(before),
    )
  })

  it('shows a red LIVE badge for live bots and DRY-RUN otherwise', async () => {
    setup([bot({ id: '1', live: true }), bot({ id: '2', symbol: 'ETH/USD', live: false })])
    expect(await screen.findByText('LIVE')).toBeInTheDocument()
    expect(screen.getByText('DRY-RUN')).toBeInTheDocument()
  })

  it('start button confirms then calls startBot', async () => {
    const { client } = setup([bot({ id: '1', status: 'created' })])
    await screen.findByText('BTC/USD')
    await userEvent.click(screen.getByRole('button', { name: /start/i }))
    // Nothing sent until the operator confirms.
    expect(client.startBot).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))
    expect(client.startBot).toHaveBeenCalledWith('1')
  })

  it('cancel in the confirm dialog aborts the action', async () => {
    const { client } = setup([bot({ id: '1', status: 'running' })])
    await screen.findByText('BTC/USD')
    await userEvent.click(screen.getByRole('button', { name: /stop/i }))
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(client.stopBot).not.toHaveBeenCalled()
  })
})
