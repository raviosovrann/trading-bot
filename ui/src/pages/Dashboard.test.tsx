import { describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
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
  onclose: (() => void) | null = null
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
  return { client, emit }
}

describe('Dashboard', () => {
  it('renders bots and updates a row on a ws decision event', async () => {
    const { emit } = setup([bot({ id: '1', symbol: 'DOGE/USD' })])
    expect(await screen.findByText('DOGE/USD')).toBeInTheDocument()
    emit({ type: 'decision', bot_id: '1', symbol: 'DOGE/USD', ts: 1, text: 'BUY signal' })
    expect(await screen.findByText(/BUY signal/)).toBeInTheDocument()
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
