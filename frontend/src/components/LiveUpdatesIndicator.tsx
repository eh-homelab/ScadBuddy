import { useRealtimeStatus } from '../lib/realtime'
import { Badge } from '@/components/ui/badge'

/**
 * #266 — shown while something on the page follows live updates and the realtime
 * socket is down; those views poll meanwhile, so it is a hint, not an error.
 */
export function LiveUpdatesIndicator() {
  const { status, following } = useRealtimeStatus()
  if (status !== 'unavailable' || !following) return null
  return (
    <Badge
      variant="secondary"
      role="status"
      title="The realtime connection is down; views refresh by polling until it is back."
      className="text-muted-foreground"
    >
      Live updates unavailable
    </Badge>
  )
}
