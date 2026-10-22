import type { OrderItemDetail } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { DataTable, type Column } from '../records/DataTable'
import { ItemStatusBadge } from './OrderStatusBadge'
import { itemPrice } from './price'

/** An order's line items (§3.3): title, status, quantity and price; a row opens the item. */
export function OrderItemsTable({
  items,
  currency,
  exponent,
  onOpen,
}: {
  items: OrderItemDetail[]
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  onOpen: (item: OrderItemDetail) => void
}) {
  const columns: Column<OrderItemDetail>[] = [
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
        return price === null ? <span className="text-muted-foreground">Not priced</span> : formatMoney(price, currency, exponent)
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
      empty="No items yet. Add one from a template's Customize page (Add to order…) or from an output (Order this)."
    />
  )
}
