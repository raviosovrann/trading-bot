import { Link } from 'react-router-dom'
import type { BotView } from '../types'
import { LiveBadge } from './LiveBadge'

function positionText(bot: BotView): string {
  const pos = bot.position
  if (!pos || pos.side === 'flat') return '—'
  return `${pos.side} ${pos.size} @ ${pos.entry_price}`
}

/** Live table of all bots; row actions delegate confirmation to the parent. */
export function BotTable({
  bots,
  onStart,
  onStop,
}: {
  bots: BotView[]
  onStart: (bot: BotView) => void
  onStop: (bot: BotView) => void
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
            <td>{bot.status}</td>
            <td>{positionText(bot)}</td>
            <td className={bot.pnl < 0 ? 'pnl-neg' : bot.pnl > 0 ? 'pnl-pos' : ''}>
              {bot.pnl.toFixed(2)}
            </td>
            <td className="muted">{bot.last_decision ?? '—'}</td>
            <td>
              {bot.status === 'running' ? (
                <button onClick={() => onStop(bot)}>Stop</button>
              ) : (
                <button onClick={() => onStart(bot)}>Start</button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
