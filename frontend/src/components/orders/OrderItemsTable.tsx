import { useId, useState, type KeyboardEvent } from 'react'
import { MoreHorizontalIcon } from 'lucide-react'
import { api } from '../../api/client'
import type { OrderAttachment, OrderItemDetail, OrderStatus, Shipment } from '../../api/types'
import { formatMoney, toMajor } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { CELL, DataTable, ROW, type Column } from '../records/DataTable'
import { ItemPicture } from './ItemPicture'
import { ItemStatusBadge } from './OrderStatusBadge'
import { itemPrice, parseUnitPrice, PRICED_WHILE, pricingQuote, UNIT_PRICE_HINT, unitPrice } from './price'
import { parseQuantity, QUANTITY_HINT } from './quantity'
import { refusalText } from './refusal'
import { CUSTOM_ITEM_TITLE, itemAnchor } from './itemLink'
import type { SliceQuoteRuns } from './useSliceQuotes'
import { UnfulfilDialog } from './Unfulfil'
import { offersUnfulfil, UNFULFIL_OFF, UNFULFIL_SETTING, unfulfilBlock } from './unfulfilRules'
import { Link } from 'react-router'

/**
 * An order's line items (§3.3): picture, title, status, quantity, unit price and line
 * total; a row opens the item. While the order is draft or quoted (§3.4) the unit price
 * is typed here, recorded and accepted as the drawer's "Record and accept" does; past
 * `quoted` the accepted prices are pinned, and shown only. A fulfilled item's row menu
 * offers Unfulfil (#4952), with a confirm step.
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
  canAdd = false,
  selected = null,
  quoting,
  onHold = false,
  shipments = [],
  unfulfilAllowed = true,
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
  /** The page offers Add item (a draft or quoted order): the empty table points at it. */
  canAdd?: boolean
  /** The item open in the page's drawer (#4693). */
  selected?: string | null
  /** The page's slice quotes: a running one shows on its row (#4693). */
  quoting?: SliceQuoteRuns
  /** The order is on hold: Unfulfil is refused (`order_on_hold`), and says why. */
  onHold?: boolean
  /** The order's shipments: Unfulfil is refused for an item one carries. */
  shipments?: Shipment[]
  /** #4953 — `orders_allow_unfulfil`: off, Unfulfil is disabled with a link to the setting. */
  unfulfilAllowed?: boolean
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
        <span className="flex flex-col items-start gap-0.5">
          <Button variant="link" className="h-auto p-0 text-left font-medium whitespace-normal text-foreground" onClick={open}>
            {item.title}
          </Button>
          {item.sku && <span className="font-mono text-[11px] text-muted-foreground">SKU {item.sku}</span>}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      cell: (item) => {
        const run = quoting?.get(item.id)
        return (
          <span className="flex flex-col items-start gap-0.5">
            <ItemStatusBadge status={item.status} />
            {run && !run.error && (
              <span className="text-[11px] text-muted-foreground" data-testid={`item-quoting-${item.id}`}>
                Quoting…
              </span>
            )}
            {run?.error && <span className="text-[11px] text-warn">Quote failed</span>}
          </span>
        )
      },
    },
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
  if (items.some((item) => offersUnfulfil({ status: orderStatus }, item))) {
    columns.push({
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      className: 'w-10 text-right',
      cell: (item) =>
        offersUnfulfil({ status: orderStatus }, item) ? (
          <ItemMenu
            orderId={orderId}
            item={item}
            blocked={unfulfilAllowed ? unfulfilBlock({ on_hold: onHold }, shipments, item) : UNFULFIL_OFF}
            off={!unfulfilAllowed}
            onChanged={onChanged}
          />
        ) : null,
    })
  }
  return (
    <DataTable
      label="Line items"
      columns={columns}
      rows={items}
      rowKey={(item) => item.id}
      rowTestId={(item) => `order-item-${item.id}`}
      rowId={(item) => itemAnchor(item.id)}
      selected={selected}
      onOpen={onOpen}
      empty={canAdd ? 'No items yet. Add one with Add item above, or type a custom item below.' : 'No items.'}
      footer={canAdd ? <CustomItemRow orderId={orderId} currency={currency} exponent={exponent} actions={columns.some((column) => column.key === 'actions')} onChanged={onChanged} /> : undefined}
    />
  )
}

/**
 * A fulfilled row's menu: Unfulfil, disabled with the reason when the backend would
 * refuse it. Named without the item's title, as the unit price's Accept is.
 */
function ItemMenu({
  orderId,
  item,
  blocked,
  off,
  onChanged,
}: {
  orderId: string
  item: OrderItemDetail
  blocked: string | null
  /** The setting turned it off: the reason links to it. */
  off: boolean
  onChanged: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  return (
    <>
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Row actions">
            <MoreHorizontalIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-auto min-w-44">
          <DropdownMenuItem variant="destructive" disabled={blocked !== null} onSelect={() => setTimeout(() => setConfirming(true), 0)}>
            Unfulfil…
          </DropdownMenuItem>
          {blocked && <DropdownMenuLabel className="max-w-56 text-xs font-normal whitespace-normal text-muted-foreground">{blocked}</DropdownMenuLabel>}
          {off && (
            <DropdownMenuItem asChild>
              <Link to={UNFULFIL_SETTING}>Open Settings → Orders</Link>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <UnfulfilDialog orderId={orderId} item={item} open={confirming} onOpenChange={setConfirming} onChanged={onChanged} />
    </>
  )
}

/**
 * #4954 — the empty row under the items: a custom item's title, quantity and unit price,
 * and optional SKU, notes and "Requires printing". Enter in any field saves it, priced
 * and accepted as it is saved (`kind: "custom"`); the row then clears for the next one.
 */
function CustomItemRow({ orderId, currency, exponent, actions, onChanged }: { orderId: string; currency: string; exponent: number; actions: boolean; onChanged: () => void }) {
  const errorId = useId()
  const blank = { title: '', quantity: '1', price: '', sku: '', notes: '', printing: false }
  const [draft, setDraft] = useState(blank)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = (fields: Partial<typeof blank>) => {
    setDraft({ ...draft, ...fields })
    setError(null)
  }

  async function submit() {
    const title = draft.title.trim()
    const quantity = parseQuantity(draft.quantity)
    const price = parseUnitPrice(draft.price, exponent)
    if (!title) return setError('Type a title for the custom item.')
    if (quantity === null) return setError(QUANTITY_HINT)
    if (price === null) return setError(UNIT_PRICE_HINT)
    setBusy(true)
    setError(null)
    try {
      await api.addOrderItem(orderId, {
        kind: 'custom',
        title,
        quantity,
        unit_price_minor: price,
        notes: draft.notes.trim(),
        ...(draft.sku.trim() ? { sku: draft.sku.trim() } : {}),
        requires_printing: draft.printing,
      })
      setDraft(blank)
      onChanged()
      document.getElementById(CUSTOM_ITEM_TITLE)?.focus()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  // A row of a table holds no form, so Enter in any field saves the row.
  function enter(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== 'Enter') return
    event.preventDefault()
    if (!busy) void submit()
  }

  const described = error ? errorId : undefined
  return (
    <tr className={ROW} data-testid="custom-item-row">
      <td className={CELL}>
        <span className="flex size-12 items-center justify-center rounded-md border border-dashed text-[10px] text-muted-foreground">Custom</span>
      </td>
      <td className={CELL}>
        <span className="flex flex-col gap-1">
          <Input
            id={CUSTOM_ITEM_TITLE}
            aria-label="Custom item title"
            aria-describedby={described}
            className="h-7"
            placeholder="Custom item: design work, a fee…"
            value={draft.title}
            onKeyDown={enter}
            onChange={(event) => set({ title: event.target.value })}
          />
          <span className="flex gap-1">
            <Input aria-label="Custom item SKU" className="h-7 w-24" placeholder="SKU" value={draft.sku} onKeyDown={enter} onChange={(event) => set({ sku: event.target.value })} />
            <Input aria-label="Custom item notes" className="h-7" placeholder="Notes" value={draft.notes} onKeyDown={enter} onChange={(event) => set({ notes: event.target.value })} />
          </span>
          {error && (
            <p id={errorId} role="alert" className="text-[11px] whitespace-normal text-warn">
              {error}
            </p>
          )}
        </span>
      </td>
      <td className={CELL}>
        <label className="flex items-center gap-1 text-[11px] whitespace-nowrap text-muted-foreground">
          <input type="checkbox" checked={draft.printing} onKeyDown={enter} onChange={(event) => set({ printing: event.target.checked })} />
          Requires printing
        </label>
      </td>
      <td className={`${CELL} sb-num`}>
        <Input aria-label="Custom item quantity" aria-describedby={described} inputMode="numeric" className="h-7 w-14 text-right" value={draft.quantity} onKeyDown={enter} onChange={(event) => set({ quantity: event.target.value })} />
      </td>
      <td className={`${CELL} sb-num`}>
        <Input aria-label="Custom item unit price" aria-describedby={described} inputMode="decimal" className="h-7 w-24 text-right" placeholder={currency} value={draft.price} onKeyDown={enter} onChange={(event) => set({ price: event.target.value })} />
      </td>
      <td className={`${CELL} sb-num`}>
        <Button type="button" size="xs" aria-label="Save custom item" disabled={busy} onClick={() => void submit()}>
          Add
        </Button>
      </td>
      {actions && <td className={CELL} />}
    </tr>
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
 * file and cost), else records a `manual` quote at it and accepts that. Escape puts back
 * the price the item has. A refusal shows under it.
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
  // Nothing to accept: the accepted quote prices the item at its quantity now.
  const priced = pricingQuote(item)
  const settled = priced !== null && priced.accepted && priced.quote.quantity === item.quantity

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
      if (item.kind === 'custom') {
        // #4954 — a custom item's price is its own field, accepted as it is saved.
        await api.updateOrderItem(orderId, item.id, { unit_price_minor: minor })
        setTyped(null)
        onChanged()
        return
      }
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
        <Button type="submit" size="xs" aria-label="Accept unit price" disabled={busy || (unchanged && settled)}>
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
