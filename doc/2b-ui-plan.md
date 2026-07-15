# 2B — UI (React SPA) Implementation Plan

> **For agentic workers:** Build **last**, against the 2A service API. TDD where practical (Vitest + React Testing Library for components/hooks; one Playwright smoke). `main` is protected — branch per task, PR through the gate.

**Goal:** An internal-team dashboard to monitor and control bots — a thin wrapper over the 2A backend, with the `LIVE`/dry-run switch front-and-center so the client can validate before funding.

**Tech Stack:** React 18 + Vite + TypeScript, React Router, TanStack Query (server state), a native `WebSocket` hook for live updates, plain CSS or a minimal lib (no heavy design system). Vitest + @testing-library/react; Playwright for one e2e.

---

## Context — the API you're consuming (from 2A)

The service exposes (all JSON; bearer-token auth except login):
- `POST /login {username, password}` → `{token}`
- `GET /bots` → `BotView[]`, `GET /bots/{id}` → `BotView`
- `POST /bots {venue, market_type, strategy, symbol, timeframe, quantity, live, per_bot_cap, params}` → `BotView`
- `PATCH /bots/{id} {live?, per_bot_cap?}` → `BotView`
- `POST /bots/{id}/start`, `POST /bots/{id}/stop`
- `GET /bots/{id}/trades` → `Trade[]`
- `GET /venues` → `{venue, market_type}[]`, `GET /strategies` → `string[]`
- `WS /ws` → stream of `{type: "decision"|"order", bot_id, ...}` events

`BotView` = `{ id, venue, market_type, strategy, symbol, timeframe, quantity, live, status, position, pnl, last_decision }`. **No secrets are ever returned** — API keys are entered at bot-creation and stored server-side only.

## Standards

- TypeScript strict mode. Types mirror the API in `src/types.ts` (single source of truth).
- All server calls go through `src/api/client.ts` (one typed wrapper, injects the bearer token); components never `fetch` directly.
- Server state via TanStack Query (caching, refetch); local UI state via `useState`. No Redux.
- Live updates via one shared `useBotEvents()` hook wrapping `WS /ws`; components subscribe to it.
- Every destructive/live action (enabling `LIVE`, start/stop) shows a confirm dialog.
- Accessible: labelled inputs, keyboard-navigable, buttons not divs.
- Vitest colocated `*.test.tsx`; mock the API client, not `fetch`, in component tests.

## File Structure

```
ui/
  index.html  vite.config.ts  package.json  tsconfig.json
  src/
    main.tsx  App.tsx  routes.tsx  types.ts
    api/client.ts            # typed fetch wrapper + token
    api/hooks.ts             # useBots, useBot, useCreateBot, usePatchBot, useStartBot, useStopBot, useTrades, useVenues, useStrategies
    hooks/useAuth.ts         # login, token in sessionStorage, logout
    hooks/useBotEvents.ts    # WS /ws subscription, fan-out
    pages/Login.tsx  pages/Dashboard.tsx  pages/BotDetail.tsx  pages/NewBot.tsx
    components/BotTable.tsx  LiveBadge.tsx  DecisionLog.tsx  ConfirmDialog.tsx  PnlSparkline.tsx
```

The FastAPI service serves `ui/dist` (2A Task A6 / this plan's Task B5).

---

## Task B1 — Scaffold + auth + typed API client

**Deliverable:**
- `npm create vite@latest ui -- --template react-ts`; add `react-router-dom`, `@tanstack/react-query`, `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `jsdom`.
- `src/types.ts` mirroring `BotView`, `Trade`, `VenueOption`, `WsEvent`.
- `src/api/client.ts`:
```ts
export function makeClient(getToken: () => string | null) {
  async function req<T>(path: string, init?: RequestInit): Promise<T> {
    const token = getToken();
    const res = await fetch(`/api${path}`, {
      ...init,
      headers: { "Content-Type": "application/json",
                 ...(token ? { Authorization: `Bearer ${token}` } : {}),
                 ...(init?.headers ?? {}) },
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.status === 204 ? (undefined as T) : res.json();
  }
  return {
    login: (u: string, p: string) => req<{ token: string }>("/login", { method: "POST", body: JSON.stringify({ username: u, password: p }) }),
    listBots: () => req<BotView[]>("/bots"),
    createBot: (b: CreateBot) => req<BotView>("/bots", { method: "POST", body: JSON.stringify(b) }),
    startBot: (id: string) => req<void>(`/bots/${id}/start`, { method: "POST" }),
    // ...stop, patch, getBot, trades, venues, strategies
  };
}
```
- `useAuth`: `login()` stores token in `sessionStorage`; `ProtectedRoute` redirects to `/login` when absent.

**Tests:** client attaches the bearer header and hits the right path (mock `fetch`); `login` stores the token; `ProtectedRoute` redirects when unauthenticated. Commit `feat/2b-scaffold`.

---

## Task B2 — Dashboard (live bots table)

**Deliverable:** `pages/Dashboard.tsx` renders `BotTable` from `useBots()`. Columns: symbol, venue, market_type, strategy, **`<LiveBadge live={...}/>`** (red "LIVE" / grey "DRY-RUN"), status, position, PnL, last signal. `useBotEvents()` patches rows live (update `last_decision`, `pnl`, `position` on matching `bot_id`). Per-row Start/Stop buttons (with `ConfirmDialog`) calling `useStartBot`/`useStopBot`. A "New bot" button → `/bots/new`.

**Tests:**
```tsx
test("renders bots and updates a row on a ws event", async () => {
  mockClient.listBots.mockResolvedValue([bot({ id: "1", symbol: "DOGE/USD" })]);
  render(<Dashboard />, { wrapper });
  expect(await screen.findByText("DOGE/USD")).toBeInTheDocument();
  emitWsEvent({ type: "decision", bot_id: "1", text: "BUY signal" });
  expect(await screen.findByText(/BUY signal/)).toBeInTheDocument();
});
test("start button confirms then calls startBot", async () => { /* click -> confirm -> mockClient.startBot called with id */ });
```
Commit `feat/2b-dashboard`.

---

## Task B3 — Bot detail

**Deliverable:** `pages/BotDetail.tsx` — read-only config summary + an edit form (`live` toggle with **confirm before enabling**, `per_bot_cap`), Start/Stop, `DecisionLog` (last N events for this bot from `useBotEvents`), `useTrades(id)` history table, `PnlSparkline`. Patches via `usePatchBot`.

**Tests:** toggling `live` on shows `ConfirmDialog`, and only PATCHes after confirm; `DecisionLog` appends when a matching WS event arrives; trades render from mocked `useTrades`. Commit `feat/2b-detail`.

---

## Task B4 — New-bot wizard (dry-run default)

**Deliverable:** `pages/NewBot.tsx` — steps: (1) venue + market_type from `useVenues()`, (2) strategy from `useStrategies()`, (3) symbol / timeframe / quantity / per_bot_cap + **API-key fields** (sent once on create, never re-fetched), (4) review. `live` defaults **false**; enabling it in the wizard requires the confirm. Submit → `useCreateBot` → navigate to the new bot's detail.

**Tests:** wizard walks the steps, POSTs the collected body with `live: false` by default; selecting a venue filters valid market types; missing required fields block "Create". Commit `feat/2b-wizard`.

---

## Task B5 — Serve SPA from FastAPI + e2e smoke

**Deliverable:** `vite build` → `ui/dist`; in `service/api.py` mount it last with SPA fallback:
```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="ui/dist", html=True), name="spa")  # after all /api routes
```
Route API under `/api` (adjust A6 routes or add an `APIRouter(prefix="/api")`) so the SPA mount at `/` doesn't shadow them. One Playwright smoke against a service started with a **fake venue** + seeded user: log in → open New-bot wizard → create a **dry-run** bot → see it in the dashboard.

**Tests:** the Playwright smoke passes headless in CI (or documented as a manual/local smoke if CI browser setup is out of scope). Commit `feat/2b-serve-and-e2e`.

## Deferred
- Rich charts beyond a sparkline; theming/dark mode; role-based views; i18n.
