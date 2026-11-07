import { useCallback, useState, useSyncExternalStore } from 'react'
import { formatDuration } from '../../lib/format'

/** One hook's clock: the time it last read, read again while something watches it. */
function createClock() {
  let now = Date.now()
  return {
    read: () => now,
    watch: (ms: number, changed: () => void) => {
      const tick = () => {
        now = Date.now()
        changed()
      }
      // #5585 — read at once: a run that starts long after mount would read its mount time
      // until the first tick. #5596 — React reads the clock again after it subscribes and
      // renders once more when it moved, so no effect sets state.
      now = Date.now()
      const timer = setInterval(tick, ms)
      return () => clearInterval(timer)
    },
  }
}

/** #5554 — `Date.now()`, read again every `ms` while `active`: a running slice's elapsed time. */
export function useNow(active: boolean, ms = 1000): number {
  const [clock] = useState(createClock)
  const subscribe = useCallback((changed: () => void) => (active ? clock.watch(ms, changed) : () => undefined), [clock, active, ms])
  return useSyncExternalStore(subscribe, clock.read)
}

/** The time from `startedAt` to `now`, never below zero: `42s`, `3m`, `1h 5m`. */
export function elapsed(startedAt: number, now: number): string {
  return formatDuration(Math.max(0, now - startedAt) / 1000)
}
