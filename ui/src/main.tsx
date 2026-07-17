import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import App from './App.tsx'
import { AuthProvider } from './hooks/useAuth'
import { BotEventsProvider } from './hooks/useBotEvents'
import './index.css'

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BotEventsProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </BotEventsProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
)
