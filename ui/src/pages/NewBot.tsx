import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useCreateBot, useStrategies, useVenues } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { LiveBadge } from '../components/LiveBadge'
import { useAuth } from '../hooks/useAuth'

interface CredField {
  name: string
  label: string
  optional?: boolean
}

// Which credential fields to collect per venue (stored via PUT secrets).
const CREDENTIAL_FIELDS: Record<string, CredField[]> = {
  coinbase: [
    { name: 'api_key', label: 'API key' },
    { name: 'api_secret', label: 'API secret' },
    { name: 'api_password', label: 'API passphrase', optional: true },
  ],
  tradovate: [
    { name: 'name', label: 'Username' },
    { name: 'password', label: 'Password' },
    { name: 'app_id', label: 'App ID' },
    { name: 'app_version', label: 'App version' },
    { name: 'cid', label: 'CID' },
    { name: 'sec', label: 'Secret' },
  ],
}

function credFieldsFor(venue: string): CredField[] {
  return CREDENTIAL_FIELDS[venue] ?? CREDENTIAL_FIELDS.coinbase
}

/** Guided bot creation: venue → strategy → params + keys → review. Dry-run default. */
export function NewBot() {
  const navigate = useNavigate()
  const { client } = useAuth()
  const venuesQuery = useVenues()
  const strategiesQuery = useStrategies()
  const createBot = useCreateBot()

  const [step, setStep] = useState(1)
  const [venue, setVenue] = useState('')
  const [marketType, setMarketType] = useState('')
  const [strategy, setStrategy] = useState('')
  const [symbol, setSymbol] = useState('')
  const [timeframe, setTimeframe] = useState('1m')
  const [quantity, setQuantity] = useState('0.1')
  const [perBotCap, setPerBotCap] = useState('1000')
  const [globalCap, setGlobalCap] = useState('10000')
  const [live, setLive] = useState(false)
  const [creds, setCreds] = useState<Record<string, string>>({})
  const [confirmLive, setConfirmLive] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const venues = useMemo(() => venuesQuery.data ?? [], [venuesQuery.data])
  const venueNames = useMemo(() => [...new Set(venues.map((v) => v.venue))], [venues])
  const marketsForVenue = useMemo(
    () => venues.filter((v) => v.venue === venue).map((v) => v.market_type),
    [venues, venue],
  )

  // Default the venue/market and strategy once their lists load.
  useEffect(() => {
    if (venue === '' && venues.length > 0) {
      setVenue(venues[0].venue)
      setMarketType(venues[0].market_type)
    }
  }, [venue, venues])
  useEffect(() => {
    const list = strategiesQuery.data
    if (strategy === '' && list && list.length > 0) setStrategy(list[0])
  }, [strategy, strategiesQuery.data])

  function onVenueChange(next: string) {
    setVenue(next)
    const markets = venues.filter((v) => v.venue === next).map((v) => v.market_type)
    setMarketType(markets[0] ?? '')
    setCreds({}) // credential fields differ per venue
  }

  const credFields = credFieldsFor(venue)
  const step3Valid =
    symbol.trim() !== '' &&
    Number(quantity) > 0 &&
    credFields.every((f) => f.optional || (creds[f.name] ?? '').trim() !== '')

  function onLiveToggle(checked: boolean) {
    if (checked) setConfirmLive(true)
    else setLive(false)
  }

  async function onCreate() {
    setError(null)
    try {
      const nonEmptyCreds = Object.fromEntries(
        Object.entries(creds).filter(([, v]) => v.trim() !== ''),
      )
      if (Object.keys(nonEmptyCreds).length > 0) {
        await client.putSecrets(venue, marketType, nonEmptyCreds)
      }
      const bot = await createBot.mutateAsync({
        venue,
        market_type: marketType,
        strategy,
        symbol: symbol.trim(),
        timeframe,
        quantity: Number(quantity),
        live,
        per_bot_cap: Number(perBotCap),
        global_cap: Number(globalCap),
        params: {},
      })
      navigate(`/bots/${bot.id}`)
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <main className="page">
      <header className="topbar">
        <h1>New bot — step {step} of 4</h1>
        <Link to="/" className="button-link">
          Cancel
        </Link>
      </header>

      <div className="card wizard">
        {step === 1 && (
          <>
            <h2>Venue</h2>
            <label htmlFor="venue">Venue</label>
            <select id="venue" value={venue} onChange={(e) => onVenueChange(e.target.value)}>
              {venueNames.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <label htmlFor="market">Market type</label>
            <select id="market" value={marketType} onChange={(e) => setMarketType(e.target.value)}>
              {marketsForVenue.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </>
        )}

        {step === 2 && (
          <>
            <h2>Strategy</h2>
            <label htmlFor="strategy">Strategy</label>
            <select id="strategy" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {(strategiesQuery.data ?? []).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </>
        )}

        {step === 3 && (
          <>
            <h2>Parameters</h2>
            <label htmlFor="symbol">Symbol</label>
            <input
              id="symbol"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              required
            />
            <label htmlFor="timeframe">Timeframe</label>
            <input
              id="timeframe"
              value={timeframe}
              onChange={(e) => setTimeframe(e.target.value)}
            />
            <label htmlFor="quantity">Quantity</label>
            <input
              id="quantity"
              inputMode="decimal"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
            />
            <label htmlFor="perBotCap">Per-bot cap ($ notional)</label>
            <input
              id="perBotCap"
              inputMode="decimal"
              value={perBotCap}
              onChange={(e) => setPerBotCap(e.target.value)}
            />
            <label htmlFor="globalCap">Global cap ($ notional)</label>
            <input
              id="globalCap"
              inputMode="decimal"
              value={globalCap}
              onChange={(e) => setGlobalCap(e.target.value)}
            />

            <h2>{venue} credentials</h2>
            <p className="muted">
              Stored server-side, encrypted at rest — sent once, never shown again.
            </p>
            {credFields.map((f) => (
              <div key={f.name}>
                <label htmlFor={`cred-${f.name}`}>
                  {f.label}
                  {f.optional ? ' (optional)' : ''}
                </label>
                <input
                  id={`cred-${f.name}`}
                  type="password"
                  autoComplete="off"
                  value={creds[f.name] ?? ''}
                  onChange={(e) => setCreds((c) => ({ ...c, [f.name]: e.target.value }))}
                />
              </div>
            ))}
          </>
        )}

        {step === 4 && (
          <>
            <h2>Review</h2>
            <dl className="config-list">
              <dt>Venue</dt>
              <dd>
                {venue} ({marketType})
              </dd>
              <dt>Strategy</dt>
              <dd>{strategy}</dd>
              <dt>Symbol</dt>
              <dd>{symbol}</dd>
              <dt>Timeframe</dt>
              <dd>{timeframe}</dd>
              <dt>Quantity</dt>
              <dd>{quantity}</dd>
              <dt>Per-bot cap</dt>
              <dd>{perBotCap}</dd>
              <dt>Mode</dt>
              <dd>
                <LiveBadge live={live} />
              </dd>
            </dl>
            <div className="control-row">
              <input
                id="live"
                type="checkbox"
                checked={live}
                onChange={(e) => onLiveToggle(e.target.checked)}
              />
              <label htmlFor="live">Enable LIVE trading (default is dry-run)</label>
            </div>
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
          </>
        )}

        <div className="button-row">
          {step > 1 && <button onClick={() => setStep((s) => s - 1)}>Back</button>}
          {step < 4 && (
            <button onClick={() => setStep((s) => s + 1)} disabled={step === 3 && !step3Valid}>
              Next
            </button>
          )}
          {step === 4 && (
            <button className="danger" onClick={onCreate} disabled={createBot.isPending}>
              {createBot.isPending ? 'Creating…' : 'Create bot'}
            </button>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmLive}
        message={`Create this bot in LIVE mode? It will send real orders to ${venue} once started.`}
        onConfirm={() => {
          setLive(true)
          setConfirmLive(false)
        }}
        onCancel={() => setConfirmLive(false)}
      />
    </main>
  )
}
