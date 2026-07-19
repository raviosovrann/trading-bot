import { Link } from 'react-router-dom'
import type { BotView } from '../types'
import { LiveBadge } from './LiveBadge'

function positionText(bot: BotView): string {
  const pos = bot.position
  if (!pos || pos.side === 'flat') return '—'
  return `${pos.side} ${pos.size} @ ${pos.entry_price}`
}

/** Statuses where the bot is mid-transition and must not be acted on again. */
const TRANSITIONAL = new Set(['starting', 'stopping'])

/** Live table of all bots; row actions delegate confirmation to the parent. */
export function BotTable({
  bots,
  onStart,
  onStop,
  busyIds = [],
}: {
  bots: BotView[]
  onStart: (bot: BotView) => void
  onStop: (bot: BotView) => void
  /** Bots with a lifecycle request already in flight from this client. */
  busyIds?: string[]
}) {
  if (bots.length === 0) {
    return <p className="muted">No bots yet — create one to get started.</p>
  }
  return (
    <table className="bot-table">
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Venue</th>
          <th>Market</th>
          <th>Strategy</th>
          <th>Mode</th>
          <th>Status</th>
          <th>Position</th>
          <th>PnL</th>
          <th>Last signal</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {bots.map((bot) => (
          <tr key={bot.id}>
            <td>
              <Link to={`/bots/${bot.id}`}>{bot.symbol}</Link>
            </td>
            <td>{bot.venue}</td>
            <td>{bot.market_type}</td>
            <td>{bot.strategy}</td>
            <td>
              <LiveBadge live={bot.live} />
            </td>
            <td>
              {bot.status}
              {bot.degraded && (
                <span className="badge-degraded" title={bot.degraded_reason ?? undefined}>
                  no data
                </span>
              )}
            </td>
            <td>{positionText(bot)}</td>
            <td className={bot.pnl < 0 ? 'pnl-neg' : bot.pnl > 0 ? 'pnl-pos' : ''}>
              {bot.pnl.toFixed(2)}
            </td>
            <td className="muted">{bot.last_decision ?? '—'}</td>
            <td>
              {(() => {
                // Busy either because this client has a request in flight, or
                // because the server reports the bot mid-transition (another
                // operator, or a reload mid-start).
                const busy = busyIds.includes(bot.id) || TRANSITIONAL.has(bot.status)
                return bot.status === 'running' ? (
                  <button disabled={busy} onClick={() => onStop(bot)}>
                    Stop
                  </button>
                ) : (
                  <button disabled={busy} onClick={() => onStart(bot)}>
                    {bot.status === 'starting' ? 'Starting…' : 'Start'}
                  </button>
                )
              })()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
