import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { BotTable } from './BotTable'
import type { BotView } from '../types'

function bot(overrides: Partial<BotView> = {}): BotView {
  return {
    id: 'bot-1',
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
    last_decision: null,
    position: null,
    pnl: 0,
    ...overrides,
  }
}

function renderTable(bots: BotView[], busyIds: string[] = []) {
  const onStart = vi.fn()
  const onStop = vi.fn()
  render(
    <MemoryRouter>
      <BotTable bots={bots} onStart={onStart} onStop={onStop} busyIds={busyIds} />
    </MemoryRouter>,
  )
  return { onStart, onStop }
}

describe('BotTable lifecycle actions', () => {
  it('disables the action while a lifecycle request is in flight', async () => {
    const { onStart } = renderTable([bot()], ['bot-1'])

    const button = screen.getByRole('button')
    expect(button).toBeDisabled()

    await userEvent.click(button)
    expect(onStart).not.toHaveBeenCalled()
  })

  it('disables the action while the bot is starting', () => {
    renderTable([bot({ status: 'starting' })])

    expect(screen.getByRole('button')).toBeDisabled()
  })

  it('disables the action while the bot is stopping', () => {
    renderTable([bot({ status: 'stopping' })])

    expect(screen.getByRole('button')).toBeDisabled()
  })

  it('offers Stop for a running bot and Start for a stopped one', () => {
    renderTable([bot({ status: 'running' }), bot({ id: 'bot-2', status: 'stopped' })])

    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled()
  })

  it('fires the action exactly once per click when idle', async () => {
    const { onStart } = renderTable([bot()])

    await userEvent.click(screen.getByRole('button', { name: 'Start' }))

    expect(onStart).toHaveBeenCalledTimes(1)
  })
})
