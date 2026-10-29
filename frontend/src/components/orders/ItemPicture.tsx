import { useState } from 'react'
import { BoxIcon } from 'lucide-react'
import type { OrderAttachment, OrderItem } from '../../api/types'
import { itemPictureSources } from './attachmentMedia'
import { cn } from '@/lib/utils'

/** A line item's picture: what was delivered once marked, the render before. */
export function ItemPicture({
  orderId,
  item,
  attachments,
  size = 'sm',
}: {
  orderId: string
  item: OrderItem
  attachments: OrderAttachment[]
  size?: 'sm' | 'lg'
}) {
  const sources = itemPictureSources(orderId, item, attachments)
  const key = sources.map((s) => s.url).join('|')
  const [failed, setFailed] = useState<{ key: string; count: number }>({ key, count: 0 })
  const count = failed.key === key ? failed.count : 0
  const source = sources[count]
  const box = size === 'sm' ? 'size-12' : 'aspect-square w-full max-w-64'
  return (
    <figure className={cn('relative shrink-0 overflow-hidden rounded-md border bg-muted', box)} data-testid={`item-picture-${item.id}`}>
      {source ? (
        <img
          key={source.url}
          src={source.url}
          alt={source.label}
          loading="lazy"
          className="size-full object-contain"
          onError={() => setFailed({ key, count: count + 1 })}
        />
      ) : item.kind === 'custom' ? (
        // #4954 — a custom item has no render: a badge stands in its place.
        <span
          role="img"
          aria-label={`${item.title} is a custom item`}
          className={cn('flex size-full items-center justify-center font-medium text-muted-foreground', size === 'sm' ? 'text-[10px]' : 'text-sm')}
          data-testid={`item-custom-${item.id}`}
        >
          Custom
        </span>
      ) : (
        <span role="img" aria-label={`No picture of ${item.title}`} className="flex size-full items-center justify-center text-muted-foreground">
          <BoxIcon aria-hidden="true" className={size === 'sm' ? 'size-5' : 'size-10'} />
        </span>
      )}
      {source?.delivered && size === 'lg' && (
        <figcaption className="absolute inset-x-0 bottom-0 bg-background/80 px-2 py-0.5 text-center text-[11px] font-medium">Delivered</figcaption>
      )}
    </figure>
  )
}
