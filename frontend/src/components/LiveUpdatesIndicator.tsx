import { useRealtimeStatus } from '../lib/realtime'

/**
 * #266 — shown while something on the page follows live updates and the realtime
 * socket is down; those views poll meanwhile, so it is a hint, not an error.
 */
export function LiveUpdatesIndicator() {
  const { status, following } = useRealtimeStatus()
  if (status !== 'unavailable' || !following) return null
  return (
    <span
      role="status"
      title="The realtime connection is down; views refresh by polling until it is back."
      className="shrink-0 rounded-[6px] bg-surface-2 px-2 py-0.5 text-[11px] text-muted"
    >
      Live updates unavailable
    </span>
  )
}
