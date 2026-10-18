import type { Activity } from '../../api/types'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { statusLabel } from '../prints/status'

type Variant = 'destructive' | 'secondary' | 'outline'

/** Every activity status names its look, so a status the API adds or renames fails typecheck. */
const VARIANT: Record<Activity['status'], Variant> = {
  queued: 'secondary',
  running: 'secondary',
  waiting: 'outline',
  succeeded: 'outline',
  failed: 'destructive',
  cancelled: 'outline',
}

/** An activity row's status, in words, as a badge; a stage's own status falls back to outline. */
export function StatusBadge({ status, className }: { status: Activity['status'] | (string & {}); className?: string }) {
  const variant = (VARIANT as Record<string, Variant | undefined>)[status] ?? 'outline'
  return (
    <Badge variant={variant} className={cn('font-normal', className)}>
      {status === 'running' && <span aria-hidden="true" className="size-1.5 animate-pulse rounded-full bg-primary" />}
      {statusLabel(status)}
    </Badge>
  )
}
