import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { makeClient, UnauthorizedError, type ApiClient } from '../api/client'
import type { SessionInfo } from '../types'

/** Auth lifecycle: 'loading' while the session is being restored on first paint. */
type AuthStatus = 'loading' | 'authed' | 'anon'

interface AuthValue {
  user: SessionInfo | null
  status: AuthStatus
  client: ApiClient
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthValue | null>(null)

/**
 * Provides auth state and the API client to the tree.
 *
 * The browser session lives in an HttpOnly cookie the SPA cannot read, so on
 * mount we ask the server (`GET /api/session`) whether a session is live and
 * restore the authenticated UI state from the response — no secret is stored in
 * the browser.
 */
export function AuthProvider({
  children,
  client: injected,
}: {
  children: ReactNode
  client?: ApiClient
}) {
  const client = useMemo(() => injected ?? makeClient(), [injected])
  const [user, setUser] = useState<SessionInfo | null>(null)
  const [status, setStatus] = useState<AuthStatus>('loading')

  useEffect(() => {
    let cancelled = false
    client
      .getSession()
      .then((info) => {
        if (!cancelled) {
          setUser(info)
          setStatus('authed')
        }
      })
      .catch(() => {
        if (!cancelled) {
          setUser(null)
          setStatus('anon')
        }
      })
    return () => {
      cancelled = true
    }
  }, [client])

  const value = useMemo<AuthValue>(
    () => ({
      user,
      status,
      client,
      async login(username, password) {
        const info = await client.login(username, password)
        setUser(info)
        setStatus('authed')
      },
      async logout() {
        try {
          await client.logout()
        } catch (err) {
          // An already-expired session still ends in the logged-out state.
          if (!(err instanceof UnauthorizedError)) throw err
        }
        setUser(null)
        setStatus('anon')
      },
    }),
    [user, status, client],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/** Access the auth context; throws if used outside an AuthProvider. */
export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
