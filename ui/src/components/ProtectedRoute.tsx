import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

/**
 * Renders nested routes only for an authenticated session.
 *
 * While the session is being restored (`loading`) nothing is rendered, so a
 * brief cookie check does not flash the login page for an already-authenticated
 * operator; an `anon` result redirects to /login.
 */
export function ProtectedRoute() {
  const { status } = useAuth()
  if (status === 'loading') return null
  return status === 'authed' ? <Outlet /> : <Navigate to="/login" replace />
}
