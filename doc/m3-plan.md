# M3 — Trading Correctness and Risk Accounting: implementation plan

M1 secured the front door. M2 made the runtime survive concurrency and failure.
M3 makes the **numbers** correct: what a bot owns, what it risked, and what it
actually traded.

Operator-facing behaviour is documented in [runbook.md](runbook.md); CI gates in
[ci.md](ci.md). M2's plan is [m2-plan.md](m2-plan.md).

---

## 1. Delivery contract

Unchanged from M2, with one amendment:

- **One branch and one PR per GitHub issue.** Branch `agent/<slug>-<issue>`.
- **Test-driven.** Every test is watched failing, for the expected reason,
  before the implementation exists. A test that passes on first run is either
  removed or justified in the PR.
- **Every commit and every PR body carries**
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` plus the Claude Code
  footer on PR bodies.
- **The agent merges** once every gate is green (`gh pr checks`,
  `mergeStateStatus` CLEAN), then syncs `main` and deletes the branch. This
  changed on 2026-07-19; M2's plan still says the user merges.
- Stacked branches: retarget with `gh pr edit N --base main` after the parent
  merges, then `gh pr close && gh pr reopen` — changing a base does not fire CI.

---

## 2. The problem underneath M3

Six of the eight issues are downstream of a single structural gap: **there is no
per-bot ledger.** The system asks the venue what it owns and infers the rest.

Measured on `main` @ `043abbe`:

| Where | What it does now | Issue |
|---|---|---|
| [`risk.py:75`](../src/tradingbot/service/risk.py) | Adds *requested* notional on any `ok` result, including `dry_run`. No per-bot cumulative — two 60-notional orders both pass a 100 cap. `close_position()` bypasses exposure release entirely. | #110 |
| [`ccxt.py:117`](../src/tradingbot/venues/ccxt.py) | Spot `get_position()` returns the **entire account base-asset balance** as this bot's position, `entry_price=0.0`. `close_position()` sells all of it. | #128 |
| [`supervisor.py:751`](../src/tradingbot/service/supervisor.py) | Persists every `OrderEvent` to `trades` — dry-run, rejected, and submitted-unfilled included. The UI calls them trades. | #135 |
| [`supervisor.py:359`](../src/tradingbot/service/supervisor.py) | `contract_multiplier` is optional on the venue protocol; ccxt has none, so derivatives silently get `1.0`. | #124 |
| [`router.py:88`](../src/tradingbot/router.py) | Maps `Action.sell` straight to `Side.sell` and never reads `Signal.position_side`. Spot is long-only in practice but nothing says so. | #125 |
| [`ccxt.py:139`](../src/tradingbot/venues/ccxt.py) | `close_position()` always sends `Side.sell`; `place_order()` drops `reduce_only` before calling `create_order()`. | #121 |

So the build order is not the issue order. The ledger comes first and the rest
is computed from it.

---

## 3. Decisions taken before starting (2026-07-19, user-confirmed)

1. **Ledger first.** `#135 → #110 → #128`. Building exposure or cost basis
   against submission responses means writing that math twice.
2. **#135 at full scope**, including the order-status poller and restart/reconnect
   reconciliation. Not split. See §5 for how that lands reviewably.
3. **#124 fails closed, and existing derivative bots may break.** A derivative
   without verified venue metadata refuses to start. A wrong multiplier
   misprices risk by orders of magnitude; a bot that will not start is a strictly
   better failure than one that silently risks 100× its cap.
4. **`BotRuntime` and the `__main__` CLI are deleted** in #177. The streaming
   runtime behind the web service is the product. This removes the polling
   runtime, its tests, and the worst-documented file in `src/`.

---

## 4. Build order

Eight issues, four clusters. Strictly sequential except where noted.

### Cluster A — the ledger (#135 → #110 → #128)

The foundation. Everything numeric depends on it.

| Issue | Work |
|---|---|
| **#135** | Durable `Order` / `Execution` (fill) / position-ledger records with venue IDs and idempotency keys. Async fill ingestion, reconciliation of open orders after restart and reconnect. Dry-run, rejected, submitted, partial, filled and cancelled all presented distinctly. `trades` stops meaning "order attempts". |
| **#110** | Exposure recomputed from the ledger: per-bot **and** global, cumulative, reservation-on-submit released on terminal state, confirmed on fill. Dry-runs consume nothing. `close_position()` releases. Check-and-update atomic. |
| **#128** | Per-bot spot lot ledger — owned qty, average cost, realized and unrealized PnL. A bot closes only what its own ledger owns. Survives restart; reconcilable against exchange balances without claiming account-wide holdings. |

**#124 lands between #135 and #110** — see below. #110's notional is
`qty × price × multiplier`, so verified metadata has to exist first.

### Cluster B — venue truth (#124 → #125)

Both introduce a description of what a venue and its instruments actually are.
Build the descriptor once, in #124, and extend it in #125.

| Issue | Work |
|---|---|
| **#124** | Resolve contract size, linear/inverse, quote currency and tick metadata from each venue's instrument metadata. Cached with an explicit refresh policy. Unknown or ambiguous derivative → bot fails validation/start. Never `1.0` by default. |
| **#125** | Venue capabilities (long/short, order types, reduce-only, market types, symbol rules, position mode) and strategy requirements. Validated at create **and** start. Router validates action against requested position side and current position. Spot short signals rejected before submission. UI cannot offer an unsupported pair. |

Note: `Strategy` ([`strategies/base.py`](../src/tradingbot/strategies/base.py))
currently declares nothing but `on_bar`. #125 adds the requirements side of the
contract to it.

### Cluster C — execution correctness (#121 → #113)

| Issue | Work |
|---|---|
| **#121** | Close long with sell, short with buy. Translate `Order.reduce_only` into CCXT's unified derivative params. Fail closed when the market cannot guarantee reduce-only — which needs #125's capability descriptor. Spot sends no derivative params. |
| **#113** | Validate venue/market pair, strategy, symbol and timeframe at create time against the registries and #125's capabilities. Reject with 4xx **before** touching supervisor or store. Make supervisor-create and store-persist transactional so a failed disk write leaves no orphan. |

#113 sits last of the functional issues deliberately: it validates the surface
that #124 and #125 define, so doing it earlier means writing the validator twice.

### Cluster D — cleanup (#177)

Last, as the issue itself instructs. Docstrings for `runtime.py`,
`tradovate_feed.py`, `stream.py`, `venues/tradovate.py`, `config.py`,
`strategy.py`, `venues/ccxt.py`, `__main__.py` up to the service package's
standard. `BotRuntime` and the CLI deleted per decision 4. No behaviour change,
no mass reformatting, no coverage-chasing.

`vulture` reports every FastAPI route and pydantic validator as unused. Those are
decorator-registered false positives and any dead-code list must filter them.

### Resulting order

```
#135 → #124 → #110 → #128 → #125 → #121 → #113 → #177
```

---

## 5. How #135 lands reviewably

At full scope #135 is the largest single PR in the project's history — durable
records, idempotency, a poller, and reconciliation. One branch, but built in
four reviewable commits:

1. Record model: `Order`, `Execution`, ledger entry. Persistence and read paths.
2. Supervisor writes fills instead of order attempts; `trades` API and UI
   distinguish dry-run / rejected / submitted / partial / filled / cancelled.
3. Order-status poller per venue, with idempotent ingestion of replayed events.
4. Restart and reconnect reconciliation of open orders.

**Check-in after commit 2.** That is the point where the data model is fixed and
everything after it is mechanical — and the last cheap moment to change the
model if it is wrong.

---

## 6. Risks specific to this milestone

- **#124 will break running derivative bots.** Accepted (decision 3), but it
  needs a runbook entry and a clear operator-facing error before the PR merges.
- **Idempotency keys are a persistence-format change.** Existing
  `data/trades/<bot>.jsonl` segments predate every record type here. The
  migration path — or the explicit decision to treat legacy segments as
  read-only history — has to be settled inside #135, not discovered in #110.
- **Reservation accounting can leak.** An order that is submitted and then never
  reaches a terminal state holds exposure forever. #110 needs an explicit
  expiry or reconciliation sweep, not just release-on-terminal.
- **Concurrency.** #110's check-and-update must be atomic; M2's TDD trap applies
  — a pre-set `asyncio.Event` never yields, so `gather()` against it does not
  reproduce a race. Put both coroutines in flight via `create_task` +
  `sleep(0)`, then release. Verify by reverting each fix individually.
