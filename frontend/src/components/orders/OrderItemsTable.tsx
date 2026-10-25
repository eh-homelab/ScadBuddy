import type { OrderAttachment, OrderItemDetail } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { DataTable, type Column } from '../records/DataTable'
import { ItemPicture } from './ItemPicture'
import { ItemStatusBadge } from './OrderStatusBadge'
import { itemPrice } from './price'

/** An order's line items (§3.3): picture, title, status, quantity and price; a row opens the item. */
export function OrderItemsTable({
  orderId,
  items,
  attachments,
  currency,
  exponent,
  onOpen,
}: {
  orderId: string
  items: OrderItemDetail[]
  /** The order's files: an item's picture is its delivered photo once one is marked (#4251). */
  attachments: OrderAttachment[]
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  onOpen: (item: OrderItemDetail) => void
}) {
  const columns: Column<OrderItemDetail>[] = [
    {
      key: 'picture',
      header: <span className="sr-only">Picture</span>,
      className: 'w-14',
      cell: (item) => <ItemPicture orderId={orderId} item={item} attachments={attachments} />,
    },
    {
      key: 'title',
      header: 'Item',
      className: 'break-words',
      cell: (item, open) => (
        <Button variant="link" className="h-auto p-0 text-left font-medium whitespace-normal text-foreground" onClick={open}>
          {item.title}
        </Button>
      ),
    },
    { key: 'status', header: 'Status', cell: (item) => <ItemStatusBadge status={item.status} /> },
    { key: 'quantity', header: 'Qty', className: 'sb-num', cell: (item) => item.quantity },
    {
      key: 'price',
      header: 'Price',
      className: 'sb-num whitespace-nowrap',
      cell: (item) => {
        const price = itemPrice(item)
        if (price === null) return <span className="text-muted-foreground">Not priced</span>
        const accepted = item.quotes.find((quote) => quote.accepted)
        return (
          <span className="flex flex-col items-end">
            {formatMoney(price.amount, currency, exponent)}
            {/* A draft's price counts before it is accepted, and says so. */}
            {!price.accepted && <span className="text-[11px] text-muted-foreground">not accepted</span>}
            {/* #4250 — a price read off a slice names the file it was read from. */}
            {accepted?.source === 'slice' && accepted.sliced_sha256 && (
              <span className="font-mono text-[11px] text-muted-foreground" title={accepted.sliced_sha256}>
                slice {accepted.sliced_sha256.slice(0, 8)}
              </span>
            )}
          </span>
        )
      },
    },
  ]
  return (
    <DataTable
      label="Line items"
      columns={columns}
      rows={items}
      rowKey={(item) => item.id}
      rowTestId={(item) => `order-item-${item.id}`}
      onOpen={onOpen}
      empty="No items yet. Add one with Add item above."
    />
  )
}
