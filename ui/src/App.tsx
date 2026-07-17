import { Route, Routes } from 'react-router-dom'
import { ProtectedRoute } from './components/ProtectedRoute'
import { BotDetail } from './pages/BotDetail'
import { Dashboard } from './pages/Dashboard'
import { Login } from './pages/Login'
import { NewBot } from './pages/NewBot'

/** Top-level route table: /login is public, everything else is protected. */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/bots/new" element={<NewBot />} />
        <Route path="/bots/:id" element={<BotDetail />} />
      </Route>
    </Routes>
  )
}
