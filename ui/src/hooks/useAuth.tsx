import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
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
  /** Central handler for a lapsed session: clear auth + caches, drop to /login. */
  onUnauthorized: () => void
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
  const queryClient = useQueryClient()
  const [user, setUser] = useState<SessionInfo | null>(null)
  const [status, setStatus] = useState<AuthStatus>('loading')

  // A lapsed/rotated session lands here from any 401 or a WS auth-close: clear
  // the authenticated state and any protected query caches so the app drops to
  // the login page and refetches cleanly on the next sign-in.
  const onUnauthorized = useCallback(() => {
    setUser(null)
    setStatus('anon')
    queryClient.clear()
  }, [queryClient])

  const client = useMemo(() => injected ?? makeClient(onUnauthorized), [injected, onUnauthorized])

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
        onUnauthorized()
      },
      onUnauthorized,
    }),
    [user, status, client, onUnauthorized],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/** Access the auth context; throws if used outside an AuthProvider. */
export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
