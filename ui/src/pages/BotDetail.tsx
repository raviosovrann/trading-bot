import { useEffect, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useBot, usePatchBot, useStartBot, useStopBot, useTrades } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { DecisionLog } from '../components/DecisionLog'
import { LiveBadge } from '../components/LiveBadge'
import { PnlSparkline } from '../components/PnlSparkline'
import { useBotEvents } from '../hooks/useBotEvents'
import type { BotView } from '../types'

interface PendingAction {
  message: string
  run: () => void
}

/** Bot detail: config, live toggle (confirmed), start/stop, log, trades, PnL. */
export function BotDetail() {
  const { id = '' } = useParams()
  const { data: bot, isLoading, error } = useBot(id)
  const { data: tradePages, fetchNextPage, hasNextPage, isFetchingNextPage } = useTrades(id)
  const patchBot = usePatchBot(id)
  const startBot = useStartBot()
  const stopBot = useStopBot()
  const queryClient = useQueryClient()
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [capInput, setCapInput] = useState<string>('')
  const [pnlSamples, setPnlSamples] = useState<number[]>([])

  useBotEvents((event) => {
    if (event.type === 'overflow') {
      // The server dropped events for this client, so the live view is
      // incomplete. Refetch rather than carry on with a partial picture.
      void queryClient.invalidateQueries({ queryKey: ['bots', id] })
      void queryClient.invalidateQueries({ queryKey: ['bots', id, 'trades'] })
      return
    }
    if (event.bot_id !== id) return
    if (event.type === 'state') {
      // The snapshot is complete and authoritative — no refetch needed.
      queryClient.setQueryData<BotView>(['bots', id], (old) =>
        old
          ? {
              ...old,
              status: event.status,
              position: event.position,
              pnl: event.pnl,
              last_decision: event.last_decision,
              degraded: event.degraded,
              degraded_reason: event.degraded_reason,
              degraded_permanent: event.degraded_permanent,
            }
          : old,
      )
      return
    }
    void queryClient.invalidateQueries({ queryKey: ['bots', id] })
    if (event.type === 'order') {
      void queryClient.invalidateQueries({ queryKey: ['bots', id, 'trades'] })
    }
  })

  const pnl = bot?.pnl
  useEffect(() => {
    if (pnl === undefined) return
    setPnlSamples((old) => (old[old.length - 1] === pnl ? old : [...old, pnl].slice(-120)))
  }, [pnl])

  if (isLoading)
    return (
      <main className="page">
        <p className="muted">Loading…</p>
      </main>
    )
  if (error || !bot) {
    return (
      <main className="page">
        <p role="alert" className="error">
          Failed to load bot: {String(error ?? 'not found')}
        </p>
        <Link to="/" className="button-link">
          Back to dashboard
        </Link>
      </main>
    )
  }

  function onLiveToggle(checked: boolean) {
    if (!bot) return
    if (checked) {
      setPending({
        message: `Enable LIVE trading for ${bot.symbol}? Real orders will be sent to ${bot.venue}.`,
        run: () => patchBot.mutate({ live: true }),
      })
    } else {
      // Turning LIVE off reduces risk — no confirmation needed.
      patchBot.mutate({ live: false })
    }
  }

  function onCapSave(e: FormEvent) {
    e.preventDefault()
    const cap = Number(capInput)
    if (Number.isFinite(cap) && cap >= 0) patchBot.mutate({ per_bot_cap: cap })
  }

  // History arrives one bounded page at a time (#122); older pages are pulled
  // on demand rather than rendered all at once.
  const trades = tradePages?.pages.flatMap((page) => page.items) ?? []

  // The server refuses configuration changes unless the bot is stopped (#109),
  // because the venue, risk guard and strategy are built once at start.
  const configurable = !['running', 'starting', 'stopping'].includes(bot.status)

  // Busy either because this client has a lifecycle request in flight, or
  // because the server reports the bot mid-transition.
  const busy =
    startBot.isPending ||
    stopBot.isPending ||
    bot.status === 'starting' ||
    bot.status === 'stopping'

  return (
    <main className="page">
      <header className="topbar">
        <h1>
          {bot.symbol} <LiveBadge live={bot.live} />
        </h1>
        <nav className="button-row">
          <Link to="/" className="button-link">
            Dashboard
          </Link>
          {bot.status === 'running' ? (
            <button
              disabled={busy}
              onClick={() =>
                setPending({ message: `Stop ${bot.symbol}?`, run: () => stopBot.mutate(bot.id) })
              }
            >
              Stop
            </button>
          ) : (
            <button
              disabled={busy}
              onClick={() =>
                setPending({
                  message: `Start ${bot.symbol} (${bot.live ? 'LIVE' : 'dry-run'})?`,
                  run: () => startBot.mutate(bot.id),
                })
              }
            >
              {bot.status === 'starting' ? 'Starting…' : 'Start'}
            </button>
          )}
        </nav>
      </header>

      <section className="detail-grid">
        <div className="card">
          <h2>Configuration</h2>
          <dl className="config-list">
            <dt>Venue</dt>
            <dd>
              {bot.venue} ({bot.market_type})
            </dd>
            <dt>Strategy</dt>
            <dd>{bot.strategy}</dd>
            <dt>Timeframe</dt>
            <dd>{bot.timeframe}</dd>
            <dt>Quantity</dt>
            <dd>{bot.quantity}</dd>
            <dt>Status</dt>
            <dd>{bot.status}</dd>
            <dt>Position</dt>
            <dd>
              {bot.position && bot.position.side !== 'flat'
                ? `${bot.position.side} ${bot.position.size} @ ${bot.position.entry_price}`
                : 'flat'}
            </dd>
            <dt>PnL</dt>
            <dd className={bot.pnl < 0 ? 'pnl-neg' : bot.pnl > 0 ? 'pnl-pos' : ''}>
              {bot.pnl.toFixed(2)} <PnlSparkline values={pnlSamples} />
            </dd>
          </dl>

          {bot.degraded && (
            <p role="status" className="warning">
              No market data is arriving — the bot is still running but is not seeing new bars.
              {bot.degraded_reason ? ` (${bot.degraded_reason})` : ''}{' '}
              {bot.degraded_permanent
                ? 'This venue cannot stream this market, so restarting will not help — use a venue that supports streaming.'
                : 'Stop and start it to re-establish the stream.'}
            </p>
          )}

          <h2>Controls</h2>
          {!configurable && (
            <p className="muted">
              Stop the bot to change its mode, caps or parameters. The venue, risk guard and
              strategy are built when the bot starts, so a change now would not reach them.
            </p>
          )}
          {patchBot.error && (
            <p role="alert" className="error">
              {String(patchBot.error)}
            </p>
          )}
          <div className="control-row">
            <input
              type="checkbox"
              id="live-toggle"
              checked={bot.live}
              disabled={!configurable}
              onChange={(e) => onLiveToggle(e.target.checked)}
            />
            <label htmlFor="live-toggle">LIVE trading (unchecked = dry-run)</label>
          </div>
          <form onSubmit={onCapSave} className="control-row">
            <label htmlFor="per-bot-cap">Per-bot cap ($ notional)</label>
            <input
              id="per-bot-cap"
              inputMode="decimal"
              disabled={!configurable}
              placeholder={String(bot.per_bot_cap)}
              value={capInput}
              onChange={(e) => setCapInput(e.target.value)}
            />
            <button type="submit" disabled={capInput === ''}>
              Save cap
            </button>
          </form>
        </div>

        <div className="card">
          <h2>Live decisions</h2>
          <DecisionLog botId={bot.id} />
        </div>
      </section>

      <section className="card">
        <h2>Trade history</h2>
        {trades.length === 0 ? (
          <p className="muted">No trades recorded yet.</p>
        ) : (
          <table className="bot-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>Status</th>
                <th>OK</th>
                <th>Order id</th>
                <th>Symbol</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={`${t.order_id ?? i}-${t.ts ?? i}`}>
                  <td>{t.ts ? new Date(t.ts).toLocaleString() : '—'}</td>
                  <td>{t.action}</td>
                  <td>{t.status}</td>
                  <td>{t.ok ? 'yes' : 'no'}</td>
                  <td>{t.order_id ?? '—'}</td>
                  <td>{t.symbol ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {hasNextPage && (
          <button onClick={() => void fetchNextPage()} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? 'Loading…' : 'Load older trades'}
          </button>
        )}
      </section>

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
