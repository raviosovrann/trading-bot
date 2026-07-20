import { describe, expect, it } from 'vitest'

import { describeTrade } from './tradeEvent'
import type { Trade } from './types'

function trade(overrides: Partial<Trade> = {}): Trade {
  return {
    bot_id: 'bot-a',
    action: '',
    status: '',
    ok: true,
    order_id: null,
    symbol: 'BTC/USD',
    ts: 1_700_000_000_000,
    seq: 1,
    kind: null,
    client_order_id: null,
    side: null,
    qty: null,
    filled_qty: null,
    avg_price: null,
    reason: null,
    ...overrides,
  }
}

describe('describeTrade', () => {
  it('labels a submission as submitted, not as a trade', () => {
    const description = describeTrade(trade({ kind: 'submitted', side: 'buy', qty: 2 }))

    expect(description.label).toBe('Submitted')
    expect(description.tone).toBe('neutral')
  })

  it('labels a fill snapshot as filled', () => {
    const description = describeTrade(
      trade({ kind: 'order_status', filled_qty: 2, avg_price: 150 }),
    )

    expect(description.label).toBe('Filled')
    expect(description.detail).toBe('2 @ 150')
    expect(description.tone).toBe('positive')
  })

  it('never shows a submission as positive', () => {
    // The whole point of #135: an acknowledged order is not a completed trade.
    expect(describeTrade(trade({ kind: 'submitted', qty: 2 })).tone).not.toBe('positive')
  })

  it('never shows a dry run as positive', () => {
    expect(describeTrade(trade({ kind: 'dry_run', qty: 2 })).tone).not.toBe('positive')
  })

  it('shows a rejection with its reason', () => {
    const description = describeTrade(
      trade({ kind: 'rejected', ok: false, reason: 'notional cap exceeded' }),
    )

    expect(description.label).toBe('Rejected')
    expect(description.detail).toBe('notional cap exceeded')
    expect(description.tone).toBe('negative')
  })

  it('labels a cancellation', () => {
    expect(describeTrade(trade({ kind: 'canceled' })).label).toBe('Canceled')
  })

  it('gives every lifecycle kind a distinct label', () => {
    const kinds = ['submitted', 'order_status', 'dry_run', 'rejected', 'canceled'] as const
    const labels = kinds.map((kind) => describeTrade(trade({ kind })).label)

    expect(new Set(labels).size).toBe(kinds.length)
  })

  it('omits price detail when the venue reported none', () => {
    const description = describeTrade(trade({ kind: 'order_status', filled_qty: 2, avg_price: 0 }))

    expect(description.detail).toBe('2')
  })

  it('shows a legacy row using the fields it actually has', () => {
    const description = describeTrade(trade({ kind: null, status: 'filled', action: 'buy' }))

    expect(description.label).toBe('filled')
    expect(description.detail).toBeNull()
  })

  it('marks a failed legacy row as negative', () => {
    expect(describeTrade(trade({ kind: null, status: 'error', ok: false })).tone).toBe('negative')
  })
})
