import { useEffect, useRef } from 'react'
import { getRealtime } from './realtime'

/**
 * Calls `read` every `ms` while the realtime socket is unavailable (#268): a page whose
 * data follows topics stays current without them. Nothing while the socket is up.
 */
export function usePollWhileUnavailable(read: () => void, ms: number): void {
  const latest = useRef(read)
  useEffect(() => {
    latest.current = read
  })
  useEffect(() => {
    const timer = setInterval(() => {
      if (getRealtime().status === 'unavailable') latest.current()
    }, ms)
    return () => clearInterval(timer)
  }, [ms])
}
