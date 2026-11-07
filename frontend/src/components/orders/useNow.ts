import { useEffect, useState } from 'react'
import { formatDuration } from '../../lib/format'

/** #5554 — `Date.now()`, read again every `ms` while `active`: a running slice's elapsed time. */
export function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    // #5585 — read at once: a run that starts long after mount would read its mount time
    // until the first tick.
    const tick = () => setNow(Date.now())
    tick()
    const timer = setInterval(tick, ms)
    return () => clearInterval(timer)
  }, [active, ms])
  return now
}

/** The time from `startedAt` to `now`, never below zero: `42s`, `3m`, `1h 5m`. */
export function elapsed(startedAt: number, now: number): string {
  return formatDuration(Math.max(0, now - startedAt) / 1000)
}
