# M2 — Reliable Runtime and Operator Control: implementation plan

Milestone **M2** hardens the bot runtime. M1 secured the front door (auth,
sessions, CI, deployment); M2 makes what runs behind it correct under
concurrency, credential rotation, and failure.

Status of the operator-facing behaviour this changes is documented in
[runbook.md](runbook.md); CI gates in [ci.md](ci.md).

---

## 1. Delivery contract

Unchanged from M1:

- **One branch and one PR per GitHub issue.** Branch name `agent/<slug>-<issue>`.
- **Test-driven.** Every test is watched failing, for the expected reason,
  before the implementation exists. A test that passes on first run is either
  removed or justified in the PR.
- **Every commit and every PR body carries**
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **The user merges.** `main` is protected; the full gate must be green
  (tests 3.11/3.12/3.13, pyright, Bandit, pip-audit, npm-audit, CodeQL, UI,
  deploy-smoke).
- Stacked branches: retarget each child with `gh pr edit N --base main` after
  its parent merges, then `gh pr close && gh pr reopen` to fire CI — changing a
  base is not a triggering event.

## 2. Clusters and order

Built A → B → C, with D free-floating.

### Cluster A — lifecycle correctness (the supervisor state machine)

| Issue | Work |
|-------|------|
| **#108** ✅ | Restore persisted bots on startup; restored bots come back `stopped`, never auto-running. Malformed records isolated. Merged in #159. |
| **#126** | Per-bot lifecycle serialization; explicit `starting`/`stopping` states; idempotent repeated start/stop; partial-startup cleanup on failure. |
| **#109** | LIVE / risk-cap / strategy-param changes on a running bot. The venue, `RiskGuard`, and strategy are all built once in `start()`, so a `PATCH` today changes the advertised config without changing what executes. |

Stacked in this order — all three edit the same `BotSupervisor` paths.

**#109 policy decision (make it before implementing):** reject
runtime-affecting patches with `409` while running, or stop/rebuild/restart
atomically. Rejecting is far smaller and cannot half-apply; an atomic restart is
friendlier but has to survive a failure mid-rebuild. Default to **409 + an
explicit stop/edit/start flow in the UI** unless the restart path proves cheap.
Whichever is chosen, the API must never advertise a config that differs from the
live execution objects.

### Cluster B — market-data plumbing

| Issue | Work |
|-------|------|
| **#112** | Shared stream lifecycle. Both concrete feeds carry global `_stopped` state and close the shared client in `finally`, so one symbol unsubscribing can kill the others, and a stopped cached hub never restarts. |
| **#137** | Invalidate cached hubs/clients when venue credentials rotate. `HubFactory` caches on `(venue, market_type, timeframe)` and never invalidates, so rotation appears to succeed but old clients keep running. |
| **#111** | Get synchronous ccxt calls off the event loop — warmup, order placement, position/balance/health. One slow exchange currently freezes every API request, WebSocket, and bot. |

#112 first: #137's invalidation needs a hub that can actually be closed and
rebuilt. #111 is independent of both and can be pulled forward if B stalls.

### Cluster C — event fan-out (depends on A)

| Issue | Work |
|-------|------|
| **#114** | Broadcast status/position/PnL. Only `decision` and `order` are emitted today, so `_task_done()` can mark a bot `failed` with the UI none the wiser. |
| **#122** | Bound the fan-out: `EventBus` gives every subscriber an unbounded queue, and `read_trades()` slurps the whole JSONL file. Add backpressure and cursor pagination. |

C follows A because the status events in #114 must emit A's new `starting`/
`stopping` states — doing it first means emitting a vocabulary that changes.
#122 follows #114 for the same reason: bound the traffic once its shape is
final.

### Cluster D — UI

| Issue | Work |
|-------|------|
| **#132** | `ConfirmDialog` focus trap/restore, Escape, inert background, and a pending state so confirm cannot fire twice. |

Independent of A–C and landable at any point. **Coordinate with #126:** that
issue's "disable or debounce matching UI actions while a request is pending" is
the same in-flight guard as #132's "confirm can trigger at most one request".
Build it once — in `ConfirmDialog` — and have #126 rely on it.

## 3. Carry-over, now tracked

- **#163 — delete/archive a bot** (M2). There was no `DELETE /api/bots/{id}` at
  all; a created bot was permanent, and since #108 it also returns on every
  restart. Open decision inside it: purge or archive `data/trades/<id>.jsonl`.
- **#164 — UI design and polish pass** (M4). Sequenced *after* #114 and #122,
  which change what the UI renders.
- **#165 — Poetry migration** (M5). Retires `requirements.txt` and
  `constraints.txt`.
- **`example` strategy is a no-op** (#78) and **Tradovate market data raises
  `NotImplementedError`** (#96). Neither blocks M2.

## 4. Definition of done

M2 is complete when all nine issues are closed, `main` is green on every gate,
and the runbook describes the resulting operator behaviour — in particular the
patch-while-running policy from #109 and the credential-rotation policy from
#137.
