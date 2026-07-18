import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
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

function Probe() {
  const { user, status, login } = useAuth()
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="user">{user?.username ?? 'none'}</span>
      <button onClick={() => void login('u', 'p')}>sign in</button>
    </div>
  )
}

describe('useAuth', () => {
  it('restores an existing session on mount', async () => {
    const client = anonClient({
      getSession: vi.fn().mockResolvedValue({ username: 'restored', roles: ['operator'] }),
    })
    render(
      <AuthProvider client={client}>
        <Probe />
      </AuthProvider>,
    )
    expect(await screen.findByText('restored')).toBeInTheDocument()
    expect(screen.getByTestId('status')).toHaveTextContent('authed')
  })

  it('sets the user on login', async () => {
    render(
      <AuthProvider client={anonClient()}>
        <Probe />
      </AuthProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anon'))
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('u')).toBeInTheDocument()
    expect(screen.getByTestId('status')).toHaveTextContent('authed')
  })
})

describe('ProtectedRoute', () => {
  it('redirects to /login when the session is anonymous', async () => {
    render(
      <AuthProvider client={anonClient()}>
        <MemoryRouter initialEntries={['/secret']}>
          <Routes>
            <Route element={<ProtectedRoute />}>
              <Route path="/secret" element={<div>secret content</div>} />
            </Route>
            <Route path="/login" element={<div>login page</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>,
    )
    expect(await screen.findByText('login page')).toBeInTheDocument()
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()
  })
})
