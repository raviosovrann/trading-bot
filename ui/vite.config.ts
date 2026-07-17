import { defineConfig, type UserConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The SPA talks to the FastAPI service under /api and /ws. In dev, proxy those
// to the local service so the same-origin paths used in production also work here.
// `test` is Vitest's config; typed loosely here to avoid the Vite/Vitest version
// skew in their respective plugin types.
const config: UserConfig & { test?: Record<string, unknown> } = {
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
    css: false,
    // Vitest owns unit tests under src/; Playwright owns e2e/.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
}

export default defineConfig(config)
