import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { VenueOption } from '../types'
import { AuthProvider } from '../hooks/useAuth'
import { NewBot } from './NewBot'

const VENUES: VenueOption[] = [
  { venue: 'coinbase', market_type: 'spot' },
  { venue: 'coinbase', market_type: 'futures' },
  { venue: 'tradovate', market_type: 'futures' },
]

function setup() {
  sessionStorage.setItem('tradingbot_token', 'tok')
  const created = { id: 'new-1' }
  const client = {
    listVenues: vi.fn().mockResolvedValue(VENUES),
    listStrategies: vi.fn().mockResolvedValue(['example']),
    putSecrets: vi.fn().mockResolvedValue(undefined),
    createBot: vi.fn().mockResolvedValue(created),
  } as unknown as ApiClient
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={['/bots/new']}>
          <Routes>
            <Route path="/bots/new" element={<NewBot />} />
            <Route path="/bots/:id" element={<div>bot detail page</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
  return { client }
}

async function next() {
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
}

beforeEach(() => sessionStorage.clear())

describe('NewBot wizard', () => {
  it('walks the steps and creates a dry-run bot by default', async () => {
    const { client } = setup()
    // Step 1: venue/market
    await screen.findByLabelText(/venue/i)
    await next()
    // Step 2: strategy
    await screen.findByLabelText(/strategy/i)
    await next()
    // Step 3: params + credentials
    await userEvent.type(screen.getByLabelText(/symbol/i), 'BTC/USD')
    await userEvent.clear(screen.getByLabelText(/quantity/i))
    await userEvent.type(screen.getByLabelText(/quantity/i), '0.1')
    await userEvent.type(screen.getByLabelText(/^API key/i), 'k')
    await userEvent.type(screen.getByLabelText(/API secret/i), 's')
    await next()
    // Step 4: review + create
    await userEvent.click(screen.getByRole('button', { name: /create/i }))

    expect(client.putSecrets).toHaveBeenCalledWith('coinbase', 'spot', expect.objectContaining({ api_key: 'k', api_secret: 's' }))
    expect(client.createBot).toHaveBeenCalledWith(expect.objectContaining({ live: false, symbol: 'BTC/USD', venue: 'coinbase' }))
    expect(await screen.findByText('bot detail page')).toBeInTheDocument()
  })

  it('filters market types to the chosen venue', async () => {
    setup()
    // Wait for the venues query to populate the market select (coinbase default).
    await screen.findByRole('option', { name: 'spot' })
    const market = () => screen.getByLabelText(/market/i) as HTMLSelectElement
    expect(Array.from(market().options).map((o) => o.value)).toEqual(['spot', 'futures'])
    // switch to tradovate → futures only (spot no longer offered)
    await userEvent.selectOptions(screen.getByLabelText(/venue/i), 'tradovate')
    expect(Array.from(market().options).map((o) => o.value)).toEqual(['futures'])
  })

  it('blocks Next on step 3 until required fields are filled', async () => {
    setup()
    await screen.findByLabelText(/venue/i)
    await next()
    await next()
    // On step 3 with empty symbol, the step's Next is disabled.
    expect(screen.getByRole('button', { name: /next/i })).toBeDisabled()
  })
})
