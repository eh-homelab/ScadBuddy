import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { statusLabel } from '../prints/status'

const VARIANT: Record<string, 'destructive' | 'secondary' | 'outline'> = {
  failed: 'destructive',
  running: 'secondary',
  queued: 'secondary',
}

/** An activity row's status, in words, as a badge. */
export function StatusBadge({ status, className }: { status: string; className?: string }) {
  return (
    <Badge variant={VARIANT[status] ?? 'outline'} className={cn('font-normal', className)}>
      {status === 'running' && <span aria-hidden="true" className="size-1.5 animate-pulse rounded-full bg-primary" />}
      {statusLabel(status)}
    </Badge>
  )
}
