import { Link, Route, Routes } from 'react-router-dom'
import { ProtectedRoute } from './components/ProtectedRoute'
import { BotDetail } from './pages/BotDetail'
import { Dashboard } from './pages/Dashboard'
import { Login } from './pages/Login'

/** Placeholder until the B4 wizard lands; keeps /bots/new off the :id route. */
function NewBotPlaceholder() {
  return (
    <main className="page">
      <p className="muted">The new-bot wizard lands in task B4.</p>
      <Link to="/" className="button-link">Back to dashboard</Link>
    </main>
  )
}

/** Top-level route table: /login is public, everything else is protected. */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/bots/new" element={<NewBotPlaceholder />} />
        <Route path="/bots/:id" element={<BotDetail />} />
      </Route>
    </Routes>
  )
}
