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

/**
 * Calls `read` every `ms` while `active`: for data that changes with no topic to say so,
 * such as a child session's call decided in its own session (#2482: the agent's
 * pending input, which the shell's badge polls too).
 */
export function usePollWhile(active: boolean, read: () => void, ms: number): void {
  const latest = useRef(read)
  useEffect(() => {
    latest.current = read
  })
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => latest.current(), ms)
    return () => clearInterval(timer)
  }, [active, ms])
}
