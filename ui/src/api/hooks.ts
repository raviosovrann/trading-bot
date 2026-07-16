import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../hooks/useAuth'
import type { CreateBot, PatchBot } from '../types'

// Thin TanStack Query wrappers over the API client. Components use these rather
// than calling the client directly, so caching and refetching live in one place.

export function useBots() {
  const { client } = useAuth()
  return useQuery({ queryKey: ['bots'], queryFn: () => client.listBots() })
}

export function useBot(id: string) {
  const { client } = useAuth()
  return useQuery({ queryKey: ['bots', id], queryFn: () => client.getBot(id) })
}

export function useTrades(id: string) {
  const { client } = useAuth()
  return useQuery({ queryKey: ['bots', id, 'trades'], queryFn: () => client.getTrades(id) })
}

export function useVenues() {
  const { client } = useAuth()
  return useQuery({ queryKey: ['venues'], queryFn: () => client.listVenues() })
}

export function useStrategies() {
  const { client } = useAuth()
  return useQuery({ queryKey: ['strategies'], queryFn: () => client.listStrategies() })
}

/** Invalidate a bot's queries (and the list) after a mutation. */
function useBotInvalidator() {
  const qc = useQueryClient()
  return (id?: string) => {
    void qc.invalidateQueries({ queryKey: ['bots'] })
    if (id) void qc.invalidateQueries({ queryKey: ['bots', id] })
  }
}

export function useCreateBot() {
  const { client } = useAuth()
  const invalidate = useBotInvalidator()
  return useMutation({
    mutationFn: (bot: CreateBot) => client.createBot(bot),
    onSuccess: () => invalidate(),
  })
}

export function usePatchBot(id: string) {
  const { client } = useAuth()
  const invalidate = useBotInvalidator()
  return useMutation({
    mutationFn: (patch: PatchBot) => client.patchBot(id, patch),
    onSuccess: () => invalidate(id),
  })
}

export function useStartBot() {
  const { client } = useAuth()
  const invalidate = useBotInvalidator()
  return useMutation({
    mutationFn: (id: string) => client.startBot(id),
    onSuccess: (bot) => invalidate(bot.id),
  })
}

export function useStopBot() {
  const { client } = useAuth()
  const invalidate = useBotInvalidator()
  return useMutation({
    mutationFn: (id: string) => client.stopBot(id),
    onSuccess: (bot) => invalidate(bot.id),
  })
}
