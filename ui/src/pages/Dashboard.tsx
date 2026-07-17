import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useBots, useStartBot, useStopBot } from '../api/hooks'
import { BotTable } from '../components/BotTable'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { useAuth } from '../hooks/useAuth'
import { useBotEvents } from '../hooks/useBotEvents'
import type { BotView } from '../types'

interface PendingAction {
  message: string
  run: () => void
}

/** Live table of all bots, updated over the WebSocket. */
export function Dashboard() {
  const { logout } = useAuth()
  const { data: bots, isLoading, error } = useBots()
  const queryClient = useQueryClient()
  const startBot = useStartBot()
  const stopBot = useStopBot()
  const [pending, setPending] = useState<PendingAction | null>(null)

  useBotEvents((event) => {
    if (event.type === 'decision') {
      // Patch the row in place — decisions arrive every bar.
      queryClient.setQueryData<BotView[]>(['bots'], (old) =>
        old?.map((b) => (b.id === event.bot_id ? { ...b, last_decision: event.text } : b)),
      )
    } else if (event.type === 'order') {
      // Fills change position/PnL/status — refetch the authoritative view.
      void queryClient.invalidateQueries({ queryKey: ['bots'] })
    }
  })

  return (
    <main className="page">
      <header className="topbar">
        <h1>Trading Console</h1>
        <nav className="button-row">
          <Link to="/bots/new" className="button-link">
            New bot
          </Link>
          <button onClick={logout}>Sign out</button>
        </nav>
      </header>

      {isLoading && <p className="muted">Loading bots…</p>}
      {error && <p role="alert" className="error">Failed to load bots: {String(error)}</p>}
      {bots && (
        <BotTable
          bots={bots}
          onStart={(bot) =>
            setPending({
              message: `Start ${bot.symbol} (${bot.live ? 'LIVE' : 'dry-run'})?`,
              run: () => startBot.mutate(bot.id),
            })
          }
          onStop={(bot) =>
            setPending({
              message: `Stop ${bot.symbol}?`,
              run: () => stopBot.mutate(bot.id),
            })
          }
        />
      )}

      <ConfirmDialog
        open={pending !== null}
        message={pending?.message ?? ''}
        onConfirm={() => {
          pending?.run()
          setPending(null)
        }}
        onCancel={() => setPending(null)}
      />
    </main>
  )
}
