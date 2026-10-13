import { act } from '@testing-library/react'
import { vi } from 'vitest'
import { getRealtime, type RealtimeListener, type RealtimeStatus } from './realtime'

/**
 * The tab's realtime client, driven by the test: a subscription is confirmed at once
 * (the server's `subscribed`) unless `confirm` is false, and `signal` stands in for
 * an event on a topic. `setStatus('unavailable')` is the socket being down.
 */
export function fakeRealtime({ confirm = true }: { confirm?: boolean } = {}) {
  const listeners = new Map<string, Set<RealtimeListener>>()
  let status: RealtimeStatus = 'live'
  const realtime = getRealtime()
  vi.spyOn(realtime, 'status', 'get').mockImplementation(() => status)
  vi.spyOn(realtime, 'subscribe').mockImplementation((topic, listener) => {
    const set = listeners.get(topic) ?? new Set<RealtimeListener>()
    set.add(listener)
    listeners.set(topic, set)
    if (confirm) queueMicrotask(() => listener('resync'))
    return () => {
      set.delete(listener)
      if (set.size === 0 && listeners.get(topic) === set) listeners.delete(topic)
    }
  })
  return {
    following: () => [...listeners.keys()],
    setStatus(next: RealtimeStatus) {
      status = next
    },
    async signal(topic: string, kind = 'event', data: Record<string, unknown> = {}) {
      await act(async () => {
        for (const listener of [...(listeners.get(topic) ?? [])]) {
          listener({ id: 'e', kind, topics: [topic], data })
        }
        await vi.advanceTimersByTimeAsync(0)
      })
    },
  }
}

