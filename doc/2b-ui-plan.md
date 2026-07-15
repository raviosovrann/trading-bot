# 2B — UI (React SPA) Implementation Plan

> **For agentic workers:** Build last, against the 2A service API. TDD where practical (component + a couple e2e smokes). The FastAPI service serves the built SPA as static files (same process, same VM).

**Goal:** An internal-team dashboard to monitor and control bots — a thin wrapper over the 2A backend, with the `LIVE`/dry-run switch front-and-center.

**Tech Stack:** React + Vite + TypeScript, a lightweight component lib (or plain CSS), a typed API client, WebSocket for live updates. Vitest + React Testing Library; one Playwright smoke.

## Global Constraints
- Talks only to the 2A REST + `WS /ws` API. No secrets in the frontend (keys are entered and stored server-side).
- Build output served by FastAPI (`service/api.py` static mount).

## File Structure
- `ui/` (Vite app): `src/api/` (typed client + WS hook), `src/pages/` (Login, Dashboard, BotDetail, NewBot), `src/components/`, `src/types.ts`
- Build step wired so `service/api.py` serves `ui/dist`.

## Tasks

### Task B1 — Scaffold + auth + API client
- **Deliverable:** Vite/React/TS app; typed API client mirroring 2A endpoints; `useAuth` (login → token, stored in memory/session); protected routes; `useBotsSocket` hook wrapping `WS /ws`.
- **Tests:** API client unit tests against a mocked fetch; login redirects unauthenticated users.

### Task B2 — Dashboard (live bots table)
- **Deliverable:** table of all bots — symbol, venue, strategy, **LIVE/dry-run badge**, status, position, PnL, last signal — updating live from `WS /ws`; start/stop buttons per row.
- **Tests:** renders rows from mocked `GET /bots`; a WS event updates a row; start/stop calls the right endpoint.

### Task B3 — Bot detail
- **Deliverable:** config form (venue, market_type, strategy, symbol, timeframe, size, risk caps, **LIVE toggle**), start/stop, live decision log (from WS), trade history (`GET /bots/{id}/trades`), a small price/PnL chart.
- **Tests:** form submits a PATCH; decision log appends on WS events; LIVE toggle shows a confirm before enabling.

### Task B4 — New-bot wizard
- **Deliverable:** step flow venue → market_type → strategy (`GET /strategies`) → symbol/timeframe/size → review; **dry-run (`LIVE=0`) default**; POSTs to `/bots`.
- **Tests:** wizard collects config and POSTs the expected body; defaults to dry-run.

### Task B5 — Serve SPA from FastAPI + e2e smoke
- **Deliverable:** build `ui/dist`; mount it in `service/api.py` (SPA fallback route); one Playwright smoke: log in → create a dry-run bot → see it in the dashboard.
- **Tests:** the Playwright smoke passes against a running service with a fake venue.

## Deferred
- Charts beyond a basic sparkline; role-based UI; theming.
