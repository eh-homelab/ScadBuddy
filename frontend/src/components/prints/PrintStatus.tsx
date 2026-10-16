import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { DELETED_STATUS, statusLabel } from './status'

const TONE: Record<string, string> = {
  completed: 'border-ok/40 bg-ok/10 text-ok',
  failed: 'border-warn/40 bg-warn/10 text-warn',
  cancelled: 'text-muted-foreground',
  printing: 'border-primary/30 bg-ui-muted',
  [DELETED_STATUS]: 'border-dashed text-muted-foreground',
}

/** A print's status as a badge; one still printing has a pulsing dot. */
export function PrintStatus({ status, className }: { status: string; className?: string }) {
  return (
    <Badge variant="outline" data-status={status} className={cn('font-normal', TONE[status], className)}>
      {status === 'printing' && (
        <span aria-hidden="true" className="size-1.5 animate-pulse rounded-full bg-primary motion-reduce:animate-none" />
      )}
      {statusLabel(status)}
    </Badge>
  )
}
