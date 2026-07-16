import { useAuth } from '../hooks/useAuth'

/** Placeholder dashboard; the live bots table lands in Task B2. */
export function Dashboard() {
  const { logout } = useAuth()
  return (
    <main className="page">
      <header className="topbar">
        <h1>Trading Console</h1>
        <button onClick={logout}>Sign out</button>
      </header>
      <p>Dashboard coming in B2.</p>
    </main>
  )
}
