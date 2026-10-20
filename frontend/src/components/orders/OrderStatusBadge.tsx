import type { Order, OrderItem } from '../../api/types'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { statusLabel } from '../prints/status'

type Variant = 'destructive' | 'secondary' | 'outline' | 'default'

const ORDER: Record<Order['status'], Variant> = {
  draft: 'secondary',
  quoted: 'secondary',
  accepted: 'default',
  in_production: 'default',
  ready: 'default',
  shipped: 'outline',
  closed: 'outline',
  cancelled: 'destructive',
}

const ITEM: Record<OrderItem['status'], Variant> = {
  draft: 'secondary',
  quoted: 'secondary',
  approved: 'default',
  queued: 'default',
  printing: 'default',
  printed: 'default',
  fulfilled: 'outline',
  failed: 'destructive',
  cancelled: 'destructive',
}

/** An order's status (§3.5) as a badge, with its hold beside it. */
export function OrderStatusBadge({ status, onHold = false, className }: { status: Order['status']; onHold?: boolean; className?: string }) {
  return (
    <span className={cn('inline-flex flex-wrap items-center gap-1', className)}>
      <Badge variant={ORDER[status]} className="font-normal">
        {statusLabel(status)}
      </Badge>
      {onHold && (
        <Badge variant="outline" className="border-warn/40 bg-warn/10 font-normal text-warn">
          On hold
        </Badge>
      )}
    </span>
  )
}

export function ItemStatusBadge({ status, className }: { status: OrderItem['status']; className?: string }) {
  return (
    <Badge variant={ITEM[status]} className={cn('font-normal', className)}>
      {statusLabel(status)}
    </Badge>
  )
}
