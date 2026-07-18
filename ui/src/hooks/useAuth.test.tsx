import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, type RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { type ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { UnauthorizedError, type ApiClient } from '../api/client'
import { AuthProvider, useAuth } from './useAuth'
import { ProtectedRoute } from '../components/ProtectedRoute'

/** A client whose session restore fails (anonymous) unless overridden. */
function anonClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    getSession: vi.fn().mockRejectedValue(new UnauthorizedError()),
    login: vi.fn().mockResolvedValue({ username: 'u', roles: ['operator'] }),
    logout: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as unknown as ApiClient
}

/** Render `ui` inside an AuthProvider wired to `client`, with a QueryClient. */
function renderWithAuth(client: ApiClient, ui: ReactNode): RenderResult {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider client={client}>{ui}</AuthProvider>
    </QueryClientProvider>,
  )
}

function Probe() {
  const { user, status, login, onUnauthorized } = useAuth()
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="user">{user?.username ?? 'none'}</span>
      <button onClick={() => void login('u', 'p')}>sign in</button>
      <button onClick={onUnauthorized}>expire</button>
    </div>
  )
}

describe('useAuth', () => {
  it('restores an existing session on mount', async () => {
    const client = anonClient({
      getSession: vi.fn().mockResolvedValue({ username: 'restored', roles: ['operator'] }),
    })
    renderWithAuth(client, <Probe />)
    expect(await screen.findByText('restored')).toBeInTheDocument()
    expect(screen.getByTestId('status')).toHaveTextContent('authed')
  })

  it('sets the user on login', async () => {
    renderWithAuth(anonClient(), <Probe />)
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'))
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('u')).toBeInTheDocument()
    expect(screen.getByTestId('status')).toHaveTextContent('authed')
  })

  it('drops to anonymous when a lapsed session is signalled', async () => {
    const client = anonClient({
      getSession: vi.fn().mockResolvedValue({ username: 'op', roles: ['operator'] }),
    })
    renderWithAuth(client, <Probe />)
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authed'))
    await userEvent.click(screen.getByRole('button', { name: /expire/i }))
    expect(screen.getByTestId('status')).toHaveTextContent('anon')
    expect(screen.getByTestId('user')).toHaveTextContent('none')
  })
})

describe('ProtectedRoute', () => {
  it('redirects to /login when the session is anonymous', async () => {
    renderWithAuth(
      anonClient(),
      <MemoryRouter initialEntries={['/secret']}>
        <Routes>
          <Route element={<ProtectedRoute />}>
            <Route path="/secret" element={<div>secret content</div>} />
          </Route>
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByText('login page')).toBeInTheDocument()
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()
  })
})
