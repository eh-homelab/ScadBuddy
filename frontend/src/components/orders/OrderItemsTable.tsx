import { useId, useState } from 'react'
import { api } from '../../api/client'
import type { OrderAttachment, OrderItemDetail, OrderStatus } from '../../api/types'
import { formatMoney, toMajor } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { DataTable, type Column } from '../records/DataTable'
import { ItemPicture } from './ItemPicture'
import { ItemStatusBadge } from './OrderStatusBadge'
import { itemPrice, parseUnitPrice, PRICED_WHILE, pricingQuote, UNIT_PRICE_HINT, unitPrice } from './price'
import { refusalText } from './refusal'

/**
 * An order's line items (§3.3): picture, title, status, quantity, unit price and line
 * total; a row opens the item. While the order is draft or quoted (§3.4) the unit price
 * is typed here, recorded and accepted as the drawer's "Record and accept" does; past
 * `quoted` the accepted prices are pinned, and shown only.
 */
export function OrderItemsTable({
  orderId,
  orderStatus,
  items,
  attachments,
  currency,
  exponent,
  onOpen,
  onChanged,
}: {
  orderId: string
  /** The order's status: a unit price is typed only while it is draft or quoted. */
  orderStatus: OrderStatus
  items: OrderItemDetail[]
  /** The order's files: an item's picture is its delivered photo once one is marked (#4251). */
  attachments: OrderAttachment[]
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  onOpen: (item: OrderItemDetail) => void
  /** After a price is recorded: the page reloads the order. */
  onChanged: () => void
}) {
  const pricing = PRICED_WHILE.has(orderStatus)
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
      key: 'unit',
      header: 'Unit price',
      className: 'sb-num whitespace-nowrap',
      cell: (item) =>
        // A cancelled item takes no price (`item_not_open`); a fulfilled one is past it.
        pricing && item.status !== 'cancelled' && item.status !== 'fulfilled' ? (
          <UnitPriceEditor orderId={orderId} item={item} currency={currency} exponent={exponent} onChanged={onChanged} />
        ) : (
          <UnitPrice item={item} currency={currency} exponent={exponent} />
        ),
    },
    {
      key: 'total',
      header: 'Line total',
      className: 'sb-num whitespace-nowrap',
      cell: (item) => {
        const price = itemPrice(item)
        if (price === null) return <span className="text-muted-foreground">Not priced</span>
        // A cancelled item is in no sum: its price shows struck through, so the column
        // adds up to the subtotal.
        if (item.status === 'cancelled') {
          return (
            <span className="flex flex-col items-end text-muted-foreground">
              <s>{formatMoney(price.amount, currency, exponent)}</s>
              <span className="text-[11px]">not counted</span>
            </span>
          )
        }
        const accepted = item.quotes.find((quote) => quote.accepted)
        return (
          <span className="flex flex-col items-end">
            {/* A draft's total counts before it is accepted; its unit price says so. */}
            <span className={price.accepted ? undefined : 'text-muted-foreground'}>{formatMoney(price.amount, currency, exponent)}</span>
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
      empty="No items yet. Add one from a template's Customize page (Add to order…) or from an output (Order this)."
    />
  )
}

/** A draft's price counts before it is accepted, and says so. */
function NotAccepted() {
  return <span className="text-[11px] text-muted-foreground">not accepted</span>
}

/** The unit price, shown only: the accepted quote's, else the newest quote's. */
function UnitPrice({ item, currency, exponent }: { item: OrderItemDetail; currency: string; exponent: number }) {
  const price = unitPrice(item)
  if (price === null) return <span className="text-muted-foreground">—</span>
  // A cancelled item's price is in no sum, accepted or not: the line total says so.
  const cancelled = item.status === 'cancelled'
  return (
    <span className={cancelled ? 'flex flex-col items-end text-muted-foreground' : 'flex flex-col items-end'}>
      {formatMoney(price.minor, currency, exponent)}
      {!price.accepted && !cancelled && <NotAccepted />}
    </span>
  )
}

/**
 * The unit price, typed: Enter (or Accept) accepts the item's pending quote when the
 * price is that quote's and it is for the item's quantity (a slice quote keeps its sliced
 * file and cost), else records a `manual` quote at it and accepts that, as `recordQuote`
 * does. Escape puts back the price the item has. A refusal shows under it.
 */
function UnitPriceEditor({
  orderId,
  item,
  currency,
  exponent,
  onChanged,
}: {
  orderId: string
  item: OrderItemDetail
  currency: string
  exponent: number
  onChanged: () => void
}) {
  const price = unitPrice(item)
  const current = price ? toMajor(price.minor, exponent) : ''
  // What was typed; null shows the item's price, so a reload brings in the new one.
  const [typed, setTyped] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const errorId = useId()
  const text = typed ?? current
  // The same amount however it is typed (12.5 is 12.50), so an accepted price stays put.
  const unchanged = price !== null && parseUnitPrice(text, exponent) === price.minor

  async function submit() {
    const minor = parseUnitPrice(text, exponent)
    if (minor === null) {
      setError(UNIT_PRICE_HINT)
      return
    }
    setBusy(true)
    setError(null)
    // The pending quote is accepted as it is only while it still prices this item: at the
    // typed price and at the item's quantity (a quantity change leaves it `quote_stale`).
    const pending = pricingQuote(item)
    const reuse = pending && !pending.accepted && pending.quote.quantity === item.quantity && pending.quote.unit_price_minor === minor ? pending.quote : null
    let recorded = false
    try {
      const quote = reuse ?? (await api.quoteOrderItem(orderId, item.id, { unit_price_minor: minor }))
      recorded = reuse === null
      await api.acceptOrderQuote(orderId, item.id, quote.id)
      setTyped(null)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
      // A refused accept after a recorded quote: reload, so the row shows it and a retry
      // accepts that price rather than recording a second one. The typed text and the
      // refusal stay.
      if (recorded) onChanged()
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="flex flex-col items-end gap-1"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <span className="flex items-center gap-1">
        <Input
          aria-label={`Unit price for ${item.title}`}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          inputMode="decimal"
          className="h-7 w-24 text-right"
          placeholder={currency}
          value={text}
          disabled={busy}
          onChange={(event) => {
            setTyped(event.target.value)
            setError(null)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              setTyped(null)
              setError(null)
            }
          }}
        />
        {/* Named without the item's title: the input beside it names the item, and a
            button named after it would match every `{ name: <title> }` lookup of the row's
            title link (Playwright matches names by substring). */}
        <Button type="submit" size="xs" aria-label="Accept unit price" disabled={busy || (unchanged && price?.accepted === true)}>
          Accept
        </Button>
      </span>
      {price && !price.accepted && <NotAccepted />}
      {error && (
        <p id={errorId} role="alert" className="max-w-56 text-right text-[11px] whitespace-normal text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
