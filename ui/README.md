# Trading Console UI

The React + TypeScript SPA for the Trading Console. It talks to the FastAPI
service under `/api` and streams live bot events over `/ws`.

**Operator and developer documentation lives in
[`../doc/runbook.md`](../doc/runbook.md)** — setup, login/session behaviour,
running bots, environment variables, and current limitations. Start there.

## Quick start

```bash
npm install     # first time only
npm run dev     # http://localhost:5173, proxies /api and /ws to :8000
```

The dev server expects the backend on `:8000`; see the runbook for how to start
it. For a single-origin setup, `npm run build` emits `dist/`, which the FastAPI
service serves at `/`.

## Scripts

| Command             | Purpose                                                           |
| ------------------- | ----------------------------------------------------------------- |
| `npm test`          | Vitest unit/component tests                                       |
| `npm run typecheck` | `tsc -b --noEmit`                                                 |
| `npm run lint`      | ESLint                                                            |
| `npm run format`    | Prettier check (`format:fix` writes)                              |
| `npm run build`     | Production bundle into `dist/`                                    |
| `npm run e2e`       | Playwright smoke (builds the SPA, serves API + bundle on `:8000`) |

`npm run e2e` drives the real backend using the repo virtualenv, so create
`.venv` and install `requirements.txt` first.

## Layout

- `src/api/` — typed API client (cookie session + CSRF header) and React Query hooks.
- `src/hooks/` — `useAuth` (session restore, central 401 handling) and
  `useBotEvents` (single WebSocket, fans events out to subscribers).
- `src/components/` — presentational pieces (bot table, decision log, confirm
  dialog, live badge, PnL sparkline).
- `src/pages/` — Login, Dashboard, BotDetail, NewBot wizard.
- `src/types.ts` — types mirroring the backend DTOs; keep in sync with
  `src/tradingbot/service/dto.py`.

Authentication is an HttpOnly cookie session — the SPA never holds a token.
State-changing requests echo the readable `tb_csrf` cookie in an
`X-CSRF-Token` header; the client does this automatically.
