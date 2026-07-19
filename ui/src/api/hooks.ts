import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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

/**
 * Trade history, one bounded page at a time.
 *
 * History grows without bound, so this never asks for all of it: pages are
 * fetched newest-first and the operator pulls older ones on demand.
 */
export function useTrades(id: string) {
  const { client } = useAuth()
  return useInfiniteQuery({
    queryKey: ['bots', id, 'trades'],
    queryFn: ({ pageParam }) => client.getTrades(id, { before: pageParam }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_cursor,
  })
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

/**
 * Delete a bot.
 *
 * The server refuses this while the bot is running (#163), so the UI both
 * disables the action and surfaces the refusal if it slips through.
 */
export function useDeleteBot() {
  const { client } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => client.deleteBot(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['bots'] }),
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
