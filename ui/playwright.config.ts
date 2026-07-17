import { defineConfig } from '@playwright/test'

// One end-to-end smoke against the FastAPI-served SPA (single origin, :8000).
// Not wired into the Python CI — run locally with `npm run e2e` after
// `.venv` is set up (the serve script uses the repo's Python + uvicorn).
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: { baseURL: 'http://127.0.0.1:8000' },
  webServer: {
    command: 'bash e2e/serve.sh',
    url: 'http://127.0.0.1:8000/',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
