import { describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { BotView, Trade, WsEvent } from '../types'
import { AuthProvider } from '../hooks/useAuth'
import { BotEventsProvider, type BotSocket } from '../hooks/useBotEvents'
import { BotDetail } from './BotDetail'

function bot(overrides: Partial<BotView> = {}): BotView {
  return {
    id: 'b1',
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

function trade(overrides: Partial<Trade> = {}): Trade {
  return {
    bot_id: 'b1',
    action: 'buy',
    status: 'submitted',
    ok: true,
    order_id: 'o1',
    symbol: 'BTC/USD',
    ts: 1700000000000,
    ...overrides,
  }
}

class FakeSocket implements BotSocket {
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  close = vi.fn()
}

function setup(theBot: BotView, trades: Trade[] = []) {
  const client = {
    getSession: vi.fn().mockResolvedValue({ username: 'op', roles: ['operator'] }),
    getBot: vi.fn().mockResolvedValue(theBot),
    getTrades: vi.fn().mockResolvedValue(trades),
    patchBot: vi.fn().mockResolvedValue({ ...theBot, live: true }),
    startBot: vi.fn().mockResolvedValue(theBot),
    stopBot: vi.fn().mockResolvedValue(theBot),
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
          <MemoryRouter initialEntries={[`/bots/${theBot.id}`]}>
            <Routes>
              <Route path="/bots/:id" element={<BotDetail />} />
            </Routes>
          </MemoryRouter>
        </BotEventsProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
  const emit = (event: WsEvent) =>
    act(() => sockets.forEach((s) => s.onmessage?.({ data: JSON.stringify(event) })))
  return { client, emit }
}

describe('BotDetail', () => {
  it('enabling LIVE shows a confirm and only PATCHes after confirming', async () => {
    const { client } = setup(bot({ live: false }))
    await screen.findByText('BTC/USD')
    await userEvent.click(screen.getByRole('checkbox', { name: /live/i }))
    expect(client.patchBot).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))
    expect(client.patchBot).toHaveBeenCalledWith('b1', expect.objectContaining({ live: true }))
  })

  it('disabling LIVE patches without a confirm', async () => {
    const { client } = setup(bot({ live: true }))
    await screen.findByText('BTC/USD')
    await userEvent.click(screen.getByRole('checkbox', { name: /live/i }))
    expect(client.patchBot).toHaveBeenCalledWith('b1', expect.objectContaining({ live: false }))
  })

  it('DecisionLog appends events for this bot and ignores others', async () => {
    const { emit } = setup(bot())
    await screen.findByText('BTC/USD')
    emit({ type: 'decision', bot_id: 'b1', symbol: 'BTC/USD', ts: 1, text: 'HOLD no cross' })
    emit({ type: 'decision', bot_id: 'other', symbol: 'ETH/USD', ts: 2, text: 'SHOULD NOT SHOW' })
    expect(await screen.findByText(/HOLD no cross/)).toBeInTheDocument()
    expect(screen.queryByText(/SHOULD NOT SHOW/)).not.toBeInTheDocument()
  })

  it('renders the trade history', async () => {
    setup(bot(), [trade({ action: 'buy', order_id: 'ord-42' })])
    expect(await screen.findByText('ord-42')).toBeInTheDocument()
  })
})

describe('BotDetail config is immutable while running (#109)', () => {
  it('disables the LIVE toggle while the bot is running', async () => {
    setup(bot({ status: 'running' }))
    await screen.findByText('BTC/USD')

    expect(screen.getByRole('checkbox', { name: /live/i })).toBeDisabled()
  })

  it('disables the cap form while the bot is running', async () => {
    setup(bot({ status: 'running' }))
    await screen.findByText('BTC/USD')

    expect(screen.getByLabelText(/per-bot cap/i)).toBeDisabled()
  })

  it('explains why the controls are unavailable while running', async () => {
    setup(bot({ status: 'running' }))
    await screen.findByText('BTC/USD')

    expect(screen.getByText(/stop the bot to change/i)).toBeInTheDocument()
  })

  it('leaves the controls usable while the bot is stopped', async () => {
    setup(bot({ status: 'stopped' }))
    await screen.findByText('BTC/USD')

    expect(screen.getByRole('checkbox', { name: /live/i })).toBeEnabled()
    expect(screen.getByLabelText(/per-bot cap/i)).toBeEnabled()
  })

  it('surfaces a rejected configuration change to the operator', async () => {
    const { client } = setup(bot({ status: 'stopped' }))
    vi.mocked(client.patchBot).mockRejectedValueOnce(
      new Error('bot is running; configuration can only be changed while stopped.'),
    )
    await screen.findByText('BTC/USD')

    await userEvent.click(screen.getByRole('checkbox', { name: /live/i }))
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/configuration can only be changed/i)
  })
})
