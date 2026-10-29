import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { OrderDetail, OrderItemDetail, OrderItemPatch } from '../api/types'
import { AddItemDialog } from '../components/orders/AddItemDialog'
import { ChargesEditor } from '../components/orders/ChargesEditor'
import { FulfilmentForm } from '../components/orders/FulfilmentForm'
import { ItemPicture } from '../components/orders/ItemPicture'
import { OrderAttachments } from '../components/orders/OrderAttachments'
import { ItemStatusBadge, OrderStatusBadge } from '../components/orders/OrderStatusBadge'
import { CUSTOM_ITEM_TITLE, itemAnchor } from '../components/orders/itemLink'
import { OrderItemsTable } from '../components/orders/OrderItemsTable'
import { itemPrice, parseUnitPrice, UNIT_PRICE_HINT, unitPrice } from '../components/orders/price'
import { QUANTITY_HINT, parseQuantity } from '../components/orders/quantity'
import { QuoteEditor } from '../components/orders/QuoteEditor'
import { refusalItems, refusalText } from '../components/orders/refusal'
import { ShipmentForm } from '../components/orders/ShipmentForm'
import { UnfulfilDialog } from '../components/orders/Unfulfil'
import { offersUnfulfil, UNFULFIL_OFF, UNFULFIL_SETTING, unfulfilBlock } from '../components/orders/unfulfilRules'
import { useSliceQuotes } from '../components/orders/useSliceQuotes'
import { RecordDrawer } from '../components/records/RecordDrawer'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { dueAt, dueDay, timeAgo } from '../lib/format'
import { formatMoney, toMajor, toMinor } from '../lib/money'
import { useAsync } from '../lib/useAsync'

const OPEN_FOR_EDITS = new Set(['draft', 'quoted'])
const ENDED = new Set(['closed', 'cancelled'])
const IN_PRODUCTION = new Set(['accepted', 'in_production', 'ready', 'shipped'])
const NONE = '__none__'
/** #4693 — the search parameter naming the open line item: `/orders/<id>?item=<item id>`. */
const ITEM_PARAM = 'item'
const ITEM_HASH = `#${itemAnchor('')}`

/** A hash's item id; a malformed escape names no item rather than failing the page. */
function decoded(text: string): string | null {
  try {
    return decodeURIComponent(text)
  } catch {
    return null
  }
}

/**
 * `/orders/:id` (spec 2026-10-18 §9): the order whole, its items in a drawer, and its audit
 * line. The open item is `?item=<id>` (#4693), so a link opens the order with that row
 * marked, scrolled to and open; `#item-<id>` is taken as the same.
 */
export function OrderPage() {
  const { id = '' } = useParams()
  const view = useAsync(() => api.order(id), [id], [`order:${id}`])
  const events = useAsync(() => api.orderEvents(id), [id], [`order:${id}`])
  // #4953 — `orders_allow_unfulfil`; on until the settings answer, as the backend's default.
  const unfulfilAllowed = useAsync(() => api.getSettings(), []).data?.orders_allow_unfulfil ?? true
  const detail = view.data
  const customer = useAsync(() => (detail ? api.customer(detail.order.customer_id) : Promise.resolve(undefined)), [detail?.order.customer_id])
  const [refusal, setRefusal] = useState<{ text: string; items: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useSearchParams()
  const location = useLocation()
  const hashItem = location.hash.startsWith(ITEM_HASH) ? decoded(location.hash.slice(ITEM_HASH.length)) : null
  const openItem = search.get(ITEM_PARAM) || hashItem || null
  // Scrolled to once per item a link names, when its row first renders: not on every
  // reload, and not for a row clicked here, which is already in view.
  const scrolled = useRef<string | null>(null)
  function setOpenItem(itemId: string | null) {
    scrolled.current = itemId
    const next = new URLSearchParams(search)
    if (itemId) next.set(ITEM_PARAM, itemId)
    else next.delete(ITEM_PARAM)
    // Replaced, so the hash form becomes the search form and Back leaves the order.
    setSearch(next, { replace: true })
  }
  const slices = useSliceQuotes(id, view.data?.items, reload)
  const loaded = view.data !== undefined
  // A link to an item the order does not have names nothing: it leaves the address bar.
  const dead = openItem !== null && view.data !== undefined && !view.data.items.some((i) => i.id === openItem)
  useEffect(() => {
    if (!dead) return
    setSearch(
      (before) => {
        const next = new URLSearchParams(before)
        next.delete(ITEM_PARAM)
        return next
      },
      { replace: true },
    )
  }, [dead, setSearch])
  useEffect(() => {
    if (!loaded || !openItem || scrolled.current === openItem) return
    const row = document.getElementById(itemAnchor(openItem))
    if (!row) return
    scrolled.current = openItem
    row.scrollIntoView?.({ block: 'center' })
  }, [loaded, openItem])
  const [adding, setAdding] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [reason, setReason] = useState('')
  const [notes, setNotes] = useState<string | null>(null)
  const [due, setDue] = useState<string | null>(null)
  const [payment, setPayment] = useState({ amount: '', method: '' })

  function reload() {
    view.refresh()
    events.refresh()
  }

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setRefusal(null)
    try {
      await action()
      reload()
    } catch (err) {
      setRefusal({ text: refusalText(err), items: refusalItems(err) })
    } finally {
      setBusy(false)
    }
  }

  if (view.error) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6">
        <h1 className="text-xl font-semibold tracking-tight">Order</h1>
        <p role="alert" className="mt-2 text-[13px] text-warn">
          Could not load this order: {view.error.message}
        </p>
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6" role="status" aria-label="Loading order">
        <h1 className="text-xl font-semibold tracking-tight">Order</h1>
        <Skeleton className="mt-3 h-6 w-1/3" />
        <Skeleton className="mt-2 h-24 w-full" />
      </div>
    )
  }
  const { order, items, totals, charges, payments, shipments, attachments } = detail
  const editable = OPEN_FOR_EDITS.has(order.status)
  const ended = ENDED.has(order.status)
  const titles = new Map(items.map((i) => [i.id, i.title]))
  const current = items.find((i) => i.id === openItem)
  const addresses = customer.data?.addresses ?? []

  return (
    <div className="h-full overflow-y-auto" data-testid="order-page">
      <div className="mx-auto flex max-w-5xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold tracking-tight">{order.number}</h1>
          <OrderStatusBadge status={order.status} onHold={order.on_hold} />
          <span className="text-sm text-muted-foreground">
            for{' '}
            <Link to={`/customers/${order.customer_id}`} className="text-primary underline-offset-4 hover:underline">
              {customer.data?.display_name ?? 'the customer'}
            </Link>
            , made {timeAgo(order.created_at)}
          </span>
        </div>

        {ended && (
          <div className="flex flex-wrap items-center gap-2" aria-label="Order actions">
            <Button variant="outline" disabled={busy} onClick={() => void act(() => api.reopenOrder(order.id))}>
              Reopen order
            </Button>
          </div>
        )}
        {!ended && (
          <div className="flex flex-wrap items-center gap-2" aria-label="Order actions">
            {editable && (
              <Button disabled={busy} onClick={() => void act(() => api.acceptOrder(order.id))}>
                Accept order
              </Button>
            )}
            {editable && (
              <Button variant="outline" disabled={busy} onClick={() => setAdding(true)}>
                Add item
              </Button>
            )}
            {editable && (
              // #4954 — the entry row under the items takes it.
              <Button variant="outline" disabled={busy} onClick={() => document.getElementById(CUSTOM_ITEM_TITLE)?.focus()}>
                Add custom item
              </Button>
            )}
            {order.status === 'shipped' && (
              <Button disabled={busy} onClick={() => void act(() => api.closeOrder(order.id))}>
                Close order
              </Button>
            )}
            {IN_PRODUCTION.has(order.status) && order.status !== 'shipped' && (
              <Button variant="outline" disabled={busy} onClick={() => void act(() => api.closeOrder(order.id))}>
                Close order
              </Button>
            )}
            <Button variant="outline" disabled={busy} onClick={() => void act(() => (order.on_hold ? api.releaseOrder(order.id) : api.holdOrder(order.id)))}>
              {order.on_hold ? 'Release hold' : 'Put on hold'}
            </Button>
            {order.status !== 'shipped' && (
              <Button variant="ghost" disabled={busy} onClick={() => setCancelling(true)}>
                Cancel order…
              </Button>
            )}
          </div>
        )}
        {cancelling && (
          <form
            className="flex flex-wrap items-end gap-2 rounded-lg border p-3"
            onSubmit={(event) => {
              event.preventDefault()
              setCancelling(false)
              void act(() => api.cancelOrder(order.id, reason.trim() || undefined))
            }}
          >
            <div className="flex flex-col gap-1">
              <Label htmlFor="cancel-reason">Why cancel {order.number}?</Label>
              <Input id="cancel-reason" className="w-64" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <Button type="submit" variant="destructive" size="sm">
              Cancel the order
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setCancelling(false)}>
              Keep it
            </Button>
          </form>
        )}
        {refusal && (
          <p role="alert" className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn" data-testid="order-refusal">
            {refusal.text}
            {refusal.items.length > 0 && <span> ({refusal.items.map((i) => titles.get(i) ?? i).join(', ')})</span>}
          </p>
        )}

        <OrderItemsTable
          orderId={order.id}
          orderStatus={order.status}
          items={items}
          attachments={attachments}
          currency={order.currency}
          exponent={order.minor_exponent}
          onOpen={(item) => setOpenItem(item.id)}
          onChanged={reload}
          canAdd={editable}
          selected={current?.id ?? null}
          quoting={slices.runs}
          onHold={order.on_hold}
          shipments={shipments}
          unfulfilAllowed={unfulfilAllowed}
        />

        <div className="grid gap-4 md:grid-cols-2">
          <Card size="sm">
            <CardContent className="flex flex-col gap-3">
              <h2 className="text-sm font-medium">Details</h2>
              <form
                className="flex flex-col gap-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  void act(() =>
                    api.updateOrder(order.id, {
                      ...(notes !== null ? { notes } : {}),
                      ...(due !== null ? { due_at: due ? dueAt(due) : null } : {}),
                    }),
                  ).then(() => {
                    setNotes(null)
                    setDue(null)
                  })
                }}
              >
                <div className="flex flex-col gap-1">
                  <Label htmlFor="order-notes">Notes</Label>
                  <Textarea id="order-notes" rows={2} disabled={ended} value={notes ?? order.notes} onChange={(e) => setNotes(e.target.value)} />
                </div>
                <div className="flex flex-wrap items-end gap-2">
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="order-due">Due</Label>
                    <Input id="order-due" type="date" className="w-40" disabled={ended} value={due ?? (order.due_at ? dueDay(order.due_at) : '')} onChange={(e) => setDue(e.target.value)} />
                  </div>
                  <Button type="submit" size="sm" variant="outline" disabled={busy || ended || (notes === null && due === null)}>
                    Save details
                  </Button>
                </div>
              </form>
              <div className="flex flex-col gap-1 text-[13px]">
                <span className="text-xs text-muted-foreground">Ship to</span>
                {editable ? (
                  <Select
                    value={order.shipping_address_id ?? NONE}
                    onValueChange={(value) => void act(() => api.updateOrder(order.id, { shipping_address_id: value === NONE ? null : value }))}
                  >
                    <SelectTrigger aria-label="Shipping address" className="w-full">
                      <SelectValue placeholder="No address" />
                    </SelectTrigger>
                    <SelectContent position="popper">
                      <SelectItem value={NONE}>No address</SelectItem>
                      {addresses.map((a) => (
                        <SelectItem key={a.id} value={a.id}>
                          {a.label ? `${a.label}: ` : ''}
                          {a.recipient}, {[...(a.lines ?? []), a.locality].filter(Boolean).join(', ')}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : order.shipping_address ? (
                  <span>
                    {order.shipping_address.recipient}, {[...(order.shipping_address.lines ?? []), order.shipping_address.locality, order.shipping_address.postal_code, order.shipping_address.country].filter(Boolean).join(', ')}
                  </span>
                ) : (
                  <span className="text-muted-foreground">No address.</span>
                )}
              </div>
            </CardContent>
          </Card>

          <Card size="sm">
            <CardContent className="flex flex-col gap-3">
              <h2 className="text-sm font-medium">Money</h2>
              <ChargesEditor orderId={order.id} currency={order.currency} exponent={order.minor_exponent} charges={charges} editable={editable} onChanged={reload} />
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[13px]" data-testid="order-totals">
                <dt className="text-muted-foreground">Items</dt>
                <dd className="sb-num text-right">{formatMoney(totals.subtotal_minor, order.currency, order.minor_exponent)}</dd>
                {(totals.pending_minor ?? 0) !== 0 && (
                  <>
                    <dt className="pl-3 text-xs text-muted-foreground">of which not accepted</dt>
                    <dd className="sb-num text-right text-xs text-muted-foreground">{formatMoney(totals.pending_minor ?? 0, order.currency, order.minor_exponent)}</dd>
                  </>
                )}
                <dt className="text-muted-foreground">Charges</dt>
                <dd className="sb-num text-right">{formatMoney(totals.charges_minor, order.currency, order.minor_exponent)}</dd>
                <dt className="font-medium">Total</dt>
                <dd className="sb-num text-right font-medium">{formatMoney(totals.total_minor, order.currency, order.minor_exponent)}</dd>
                <dt className="text-muted-foreground">Paid</dt>
                <dd className="sb-num text-right">{formatMoney(totals.paid_minor, order.currency, order.minor_exponent)}</dd>
                <dt className="text-muted-foreground">{(totals.pending_minor ?? 0) !== 0 ? 'Balance due (provisional)' : 'Balance due'}</dt>
                <dd className="sb-num text-right">{formatMoney(totals.balance_minor, order.currency, order.minor_exponent)}</dd>
              </dl>
              {(totals.unpriced_items ?? []).length > 0 && (
                <p className="text-xs text-warn" data-testid="order-unpriced">
                  Not in the total, with no price yet: {(totals.unpriced_items ?? []).map((id) => titles.get(id) ?? id).join(', ')}
                </p>
              )}
              {payments.length > 0 && (
                <ul className="flex flex-col gap-0.5 text-[13px]" aria-label="Payments">
                  {payments.map((p) => (
                    <li key={p.id} className="flex justify-between gap-2">
                      <span>
                        {p.method ?? 'Payment'} {p.reference ? `(${p.reference})` : ''} · {timeAgo(p.created_at)}
                      </span>
                      <span className="sb-num">{formatMoney(p.amount_minor, order.currency, order.minor_exponent)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {order.status !== 'cancelled' && (
                <form
                  className="flex flex-wrap items-end gap-2"
                  onSubmit={(event) => {
                    event.preventDefault()
                    const minor = toMinor(payment.amount, order.minor_exponent)
                    if (minor === null) {
                      setRefusal({ text: 'Enter the amount received, such as 12.50.', items: [] })
                      return
                    }
                    void act(() => api.addPayment(order.id, { amount_minor: minor, method: payment.method.trim() || null })).then(() => setPayment({ amount: '', method: '' }))
                  }}
                >
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="payment-amount">Received ({order.currency})</Label>
                    <Input id="payment-amount" inputMode="decimal" className="w-28" value={payment.amount} onChange={(e) => setPayment({ ...payment, amount: e.target.value })} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="payment-method">Method</Label>
                    <Input id="payment-method" className="w-28" placeholder="cash, card" value={payment.method} onChange={(e) => setPayment({ ...payment, method: e.target.value })} />
                  </div>
                  <Button type="submit" size="sm" variant="outline" disabled={busy}>
                    Record payment
                  </Button>
                </form>
              )}
            </CardContent>
          </Card>
        </div>

        <Card size="sm">
          <CardContent>
            <OrderAttachments orderId={order.id} attachments={attachments.filter((a) => a.item_id === null)} onChanged={reload} />
          </CardContent>
        </Card>

        <Card size="sm">
          <CardContent>
            <ShipmentForm orderId={order.id} currency={order.currency} exponent={order.minor_exponent} items={items} shipments={shipments} enabled={IN_PRODUCTION.has(order.status) && !order.on_hold} onChanged={reload} />
          </CardContent>
        </Card>

        <section className="flex flex-col gap-1" aria-label="Audit line">
          <h2 className="text-sm font-medium">History</h2>
          {events.data && events.data.length > 0 ? (
            <ol className="flex flex-col gap-0.5 text-[13px] text-muted-foreground">
              {[...events.data].reverse().map((e) => (
                <li key={e.id}>
                  <span className="text-foreground">{e.kind}</span>
                  {typeof e.detail.to === 'string' ? ` → ${e.detail.to}` : ''} · {timeAgo(e.at)} · {e.by.kind === 'agent' ? (e.by.principal ?? 'agent') : 'browser'}
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-[13px] text-muted-foreground">Nothing yet.</p>
          )}
        </section>
      </div>
      {editable && <AddItemDialog open={adding} orderId={order.id} onAdded={reload} onClose={() => setAdding(false)} />}
      {current && (
        <ItemDrawer key={current.id} detail={detail} item={current} slices={slices} unfulfilAllowed={unfulfilAllowed} onClose={() => setOpenItem(null)} onChanged={reload} />
      )}
    </div>
  )
}

function ItemDrawer({
  detail,
  item,
  slices,
  unfulfilAllowed,
  onClose,
  onChanged,
}: {
  detail: OrderDetail
  item: OrderItemDetail
  slices: Pick<ReturnType<typeof useSliceQuotes>, 'runs' | 'start' | 'dismiss'>
  /** #4953 — `orders_allow_unfulfil`. */
  unfulfilAllowed: boolean
  onClose: () => void
  onChanged: () => void
}) {
  const { order } = detail
  const editable = OPEN_FOR_EDITS.has(order.status)
  const settled = item.status === 'fulfilled' || item.status === 'cancelled'
  const [error, setError] = useState<string | null>(null)
  const [unfulfilling, setUnfulfilling] = useState(false)
  const unfulfilBlocked = unfulfilAllowed ? unfulfilBlock(order, detail.shipments, item) : UNFULFIL_OFF
  const custom = item.kind === 'custom'
  const unit = unitPrice(item)
  const unitText = unit ? toMajor(unit.minor, order.minor_exponent) : ''
  const [quantity, setQuantity] = useState(String(item.quantity))
  const [title, setTitle] = useState(item.title)
  const [sku, setSku] = useState(item.sku ?? '')
  const [notes, setNotes] = useState(item.notes)
  const [typedPrice, setTypedPrice] = useState(unitText)
  const [printing, setPrinting] = useState(item.requires_printing)
  useEffect(() => {
    setQuantity(String(item.quantity))
    setTitle(item.title)
    setSku(item.sku ?? '')
    setNotes(item.notes)
    setTypedPrice(unitText)
    setPrinting(item.requires_printing)
  }, [item.quantity, item.title, item.sku, item.notes, unitText, item.requires_printing])
  const price = itemPrice(item)
  const changed =
    quantity !== String(item.quantity) ||
    title !== item.title ||
    sku !== (item.sku ?? '') ||
    notes !== item.notes ||
    (custom && (typedPrice !== unitText || printing !== item.requires_printing))

  /** Only the fields that changed: a quantity or price sent again would reprice the item. */
  function patch(): OrderItemPatch | string {
    const count = parseQuantity(quantity)
    if (count === null) return QUANTITY_HINT
    const body: OrderItemPatch = {}
    if (count !== item.quantity) body.quantity = count
    if (title.trim() !== item.title) body.title = title.trim()
    if (sku.trim() !== (item.sku ?? '')) body.sku = sku.trim() || null
    if (notes !== item.notes) body.notes = notes
    if (custom) {
      if (typedPrice !== unitText) {
        const minor = parseUnitPrice(typedPrice, order.minor_exponent)
        if (minor === null) return UNIT_PRICE_HINT
        body.unit_price_minor = minor
      }
      if (printing !== item.requires_printing) body.requires_printing = printing
    }
    return body
  }

  async function run(action: () => Promise<unknown>, close = false) {
    setError(null)
    try {
      await action()
      onChanged()
      if (close) onClose()
    } catch (err) {
      setError(refusalText(err))
    }
  }

  return (
    <RecordDrawer
      title={item.title}
      onClose={onClose}
      actions={
        <>
          <ItemStatusBadge status={item.status} />
          {item.reference ? (
            <span className="text-xs text-muted-foreground">
              {item.reference.slug} @ {item.reference.commit.slice(0, 7)}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">Custom item{item.requires_printing ? ', requires printing' : ''}</span>
          )}
          {item.output_id && item.reference && (
            <Link to={`/m/${item.reference.slug}/history`} className="ml-auto text-xs text-primary underline-offset-4 hover:underline">
              From an output
            </Link>
          )}
        </>
      }
      footer={
        offersUnfulfil(order, item) ? (
          <>
            <Button variant="outline" disabled={unfulfilBlocked !== null} onClick={() => setUnfulfilling(true)}>
              Unfulfil…
            </Button>
            {unfulfilBlocked && (
              <p className="text-xs text-muted-foreground" data-testid="unfulfil-blocked">
                {unfulfilBlocked}
                {!unfulfilAllowed && (
                  <>
                    {' '}
                    <Link to={UNFULFIL_SETTING} className="text-primary underline-offset-4 hover:underline">
                      Open Settings
                    </Link>
                  </>
                )}
              </p>
            )}
            <UnfulfilDialog orderId={order.id} item={item} open={unfulfilling} onOpenChange={setUnfulfilling} onChanged={onChanged} />
          </>
        ) : item.status === 'cancelled' && !ENDED.has(order.status) ? (
          editable || item.quotes.some((q) => q.accepted) ? (
            <Button variant="outline" onClick={() => void run(() => api.reopenOrderItem(order.id, item.id))}>
              Reopen item
            </Button>
          ) : (
            // `item_not_priced`: an accepted order takes back a priced item only.
            <p className="text-xs text-muted-foreground" data-testid="reopen-unpriced">
              This item had no accepted price, so it can't come back into an accepted order.
            </p>
          )
        ) : (
          !settled &&
          !ENDED.has(order.status) && (
          <>
            {item.status === 'draft' ? (
              <Button variant="ghost" onClick={() => void run(() => api.deleteOrderItem(order.id, item.id), true)}>
                Delete item
              </Button>
            ) : (
              <Button variant="ghost" onClick={() => void run(() => api.cancelOrderItem(order.id, item.id))}>
                Cancel item
              </Button>
            )}
          </>
          )
        )
      }
    >
      <ItemPicture orderId={order.id} item={item} attachments={detail.attachments} size="lg" />
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          const body = patch()
          if (typeof body === 'string') {
            setError(body)
            return
          }
          void run(() => api.updateOrderItem(order.id, item.id, body))
        }}
      >
        <div className="flex flex-col gap-1">
          <Label htmlFor="item-title">Title</Label>
          <Input id="item-title" className="w-56" disabled={settled || ENDED.has(order.status)} value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="item-quantity">Quantity</Label>
          <Input id="item-quantity" inputMode="numeric" className="w-20" disabled={!editable || settled} value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </div>
        {custom && (
          <div className="flex flex-col gap-1">
            <Label htmlFor="item-price">Price per unit ({order.currency})</Label>
            <Input id="item-price" inputMode="decimal" className="w-28" disabled={!editable || settled} value={typedPrice} onChange={(e) => setTypedPrice(e.target.value)} />
          </div>
        )}
        <div className="flex flex-col gap-1">
          <Label htmlFor="item-sku">SKU</Label>
          <Input id="item-sku" className="w-28" disabled={settled || ENDED.has(order.status)} value={sku} onChange={(e) => setSku(e.target.value)} />
        </div>
        <div className="flex w-full flex-col gap-1">
          <Label htmlFor="item-notes">Notes</Label>
          <Textarea id="item-notes" rows={2} disabled={settled || ENDED.has(order.status)} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
        {custom && (
          <label className="flex items-center gap-2 text-[13px]">
            <Checkbox checked={printing} disabled={settled || ENDED.has(order.status)} onCheckedChange={(next) => setPrinting(next === true)} />
            Requires printing
          </label>
        )}
        <Button type="submit" size="sm" variant="outline" disabled={settled || ENDED.has(order.status) || !changed}>
          Save item
        </Button>
      </form>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[13px]">
        <dt className="text-muted-foreground">Price</dt>
        <dd className="sb-num">
          {price === null ? 'Not priced' : formatMoney(price.amount, order.currency, order.minor_exponent)}
          {price && !price.accepted && <span className="ml-1 text-xs text-muted-foreground">(not accepted)</span>}
        </dd>
        {item.reference && (
          <>
            <dt className="text-muted-foreground">Inputs</dt>
            <dd className="break-all font-mono text-xs">{JSON.stringify((item.reference.inputs as { params?: unknown }).params ?? {})}</dd>
          </>
        )}
        {item.colours.length > 0 && (
          <>
            <dt className="text-muted-foreground">Colours</dt>
            <dd className="flex flex-wrap gap-1">
              {item.colours.map((c) => (
                <span key={c.slot} className="inline-flex items-center gap-1 text-xs">
                  <span aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: c.colour }} />
                  {c.colour}
                </span>
              ))}
            </dd>
          </>
        )}
      </dl>
      {custom ? (
        // #4954 — a custom item's price is the field above: its quotes are shown only.
        <QuoteEditor orderId={order.id} itemId={item.id} currency={order.currency} exponent={order.minor_exponent} quotes={item.quotes} editable={false} onChanged={onChanged} />
      ) : (
        <QuoteEditor orderId={order.id} itemId={item.id} currency={order.currency} exponent={order.minor_exponent} quotes={item.quotes} editable={editable && !settled} onChanged={onChanged} item={item} slice={{ run: slices.runs.get(item.id), start: (body) => slices.start(item.id, body, item.quotes.map((q) => q.id)), dismiss: () => slices.dismiss(item.id) }} />
      )}
      <FulfilmentForm orderId={order.id} itemId={item.id} quantity={item.quantity} attempts={item.fulfilments} enabled={IN_PRODUCTION.has(order.status) && !order.on_hold && !settled} onChanged={onChanged} markDone={custom && !item.requires_printing} />
      <OrderAttachments
        orderId={order.id}
        itemId={item.id}
        attachments={detail.attachments.filter((a) => a.item_id === item.id)}
        deliveredId={item.delivered_attachment_id ?? null}
        onChanged={onChanged}
      />
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </RecordDrawer>
  )
}
