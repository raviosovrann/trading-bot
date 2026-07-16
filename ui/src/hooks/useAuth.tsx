import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { makeClient, type ApiClient } from '../api/client'

const TOKEN_KEY = 'tradingbot_token'

interface AuthValue {
  token: string | null
  client: ApiClient
  login: (username: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthValue | null>(null)

/** Provides auth state and a token-injecting API client to the tree. */
export function AuthProvider({
  children,
  client: injected,
}: {
  children: ReactNode
  client?: ApiClient
}) {
  const [token, setToken] = useState<string | null>(() => sessionStorage.getItem(TOKEN_KEY))
  // A stable client that always reads the freshest token from sessionStorage.
  const client = useMemo(
    () => injected ?? makeClient(() => sessionStorage.getItem(TOKEN_KEY)),
    [injected],
  )

  const value = useMemo<AuthValue>(
    () => ({
      token,
      client,
      async login(username, password) {
        const { token: minted } = await client.login(username, password)
        sessionStorage.setItem(TOKEN_KEY, minted)
        setToken(minted)
      },
      logout() {
        sessionStorage.removeItem(TOKEN_KEY)
        setToken(null)
      },
    }),
    [token, client],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/** Access the auth context; throws if used outside an AuthProvider. */
export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
