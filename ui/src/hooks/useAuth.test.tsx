import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ApiClient } from '../api/client'
import { AuthProvider, useAuth } from './useAuth'
import { ProtectedRoute } from '../components/ProtectedRoute'

function fakeClient(): ApiClient {
  return { login: vi.fn().mockResolvedValue({ token: 'tok-abc' }) } as unknown as ApiClient
}

function Probe() {
  const { token, login } = useAuth()
  return (
    <div>
      <span data-testid="token">{token ?? 'none'}</span>
      <button onClick={() => void login('u', 'p')}>sign in</button>
    </div>
  )
}

beforeEach(() => sessionStorage.clear())

describe('useAuth', () => {
  it('stores the token on login', async () => {
    render(
      <AuthProvider client={fakeClient()}>
        <Probe />
      </AuthProvider>,
    )
    expect(screen.getByTestId('token')).toHaveTextContent('none')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText('tok-abc')).toBeInTheDocument()
    expect(sessionStorage.getItem('tradingbot_token')).toBe('tok-abc')
  })
})

describe('ProtectedRoute', () => {
  it('redirects to /login when unauthenticated', () => {
    render(
      <AuthProvider client={fakeClient()}>
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
    expect(screen.getByText('login page')).toBeInTheDocument()
    expect(screen.queryByText('secret content')).not.toBeInTheDocument()
  })
})
