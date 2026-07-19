import { useState } from 'react'
import { useBotEvents } from '../hooks/useBotEvents'
import type { WsEvent } from '../types'

// State snapshots (#114) are rendered as fields elsewhere, not as log lines.
type LoggableEvent = Exclude<WsEvent, { type: 'state' } | { type: 'overflow' }>

function describe(event: LoggableEvent): string {
  if (event.type === 'decision') {
    const when = event.ts > 0 ? `${new Date(event.ts).toLocaleTimeString()} ` : ''
    return `${when}${event.text}`
  }
  return `order ${event.action} → ${event.status}${event.ok ? '' : ' (failed)'}`
}

/** Rolling log of this bot's live decision/order events, newest first. */
export function DecisionLog({ botId, limit = 50 }: { botId: string; limit?: number }) {
  const [entries, setEntries] = useState<string[]>([])

  useBotEvents((event) => {
    if (event.type === 'state' || event.type === 'overflow') return
    if (event.bot_id !== botId) return
    setEntries((old) => [describe(event), ...old].slice(0, limit))
  })

  if (entries.length === 0) {
    return <p className="muted">No live events yet — they appear as the bot runs.</p>
  }
  return (
    <ul className="decision-log" aria-label="Decision log">
      {entries.map((entry, i) => (
        <li key={`${entries.length - i}`}>{entry}</li>
      ))}
    </ul>
  )
}
