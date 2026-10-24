import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import { api } from '../api/client'
import type { OrderDetail, OrderItemDetail } from '../api/types'
import { ChargesEditor } from '../components/orders/ChargesEditor'
import { FulfilmentForm } from '../components/orders/FulfilmentForm'
import { ItemPicture } from '../components/orders/ItemPicture'
import { OrderAttachments } from '../components/orders/OrderAttachments'
import { ItemStatusBadge, OrderStatusBadge } from '../components/orders/OrderStatusBadge'
import { OrderItemsTable } from '../components/orders/OrderItemsTable'
import { itemPrice } from '../components/orders/price'
import { QuoteEditor } from '../components/orders/QuoteEditor'
import { refusalItems, refusalText } from '../components/orders/refusal'
import { ShipmentForm } from '../components/orders/ShipmentForm'
import { RecordDrawer } from '../components/records/RecordDrawer'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { timeAgo } from '../lib/format'
import { formatMoney, toMinor } from '../lib/money'
import { useAsync } from '../lib/useAsync'

const OPEN_FOR_EDITS = new Set(['draft', 'quoted'])
const ENDED = new Set(['closed', 'cancelled'])
const IN_PRODUCTION = new Set(['accepted', 'in_production', 'ready', 'shipped'])
const NONE = '__none__'

/** `/orders/:id` (spec 2026-10-18 §9): the order whole, its items in a drawer, and its audit line. */
export function OrderPage() {
  const { id = '' } = useParams()
  const view = useAsync(() => api.order(id), [id], [`order:${id}`])
  const events = useAsync(() => api.orderEvents(id), [id], [`order:${id}`])
  const detail = view.data
  const customer = useAsync(() => (detail ? api.customer(detail.order.customer_id) : Promise.resolve(undefined)), [detail?.order.customer_id])
  const [refusal, setRefusal] = useState<{ text: string; items: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [openItem, setOpenItem] = useState<string | null>(null)
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

        <OrderItemsTable orderId={order.id} items={items} attachments={attachments} currency={order.currency} exponent={order.minor_exponent} onOpen={(item) => setOpenItem(item.id)} />

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
                      ...(due !== null ? { due_at: due ? `${due}T00:00:00Z` : null } : {}),
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
                    <Input id="order-due" type="date" className="w-40" disabled={ended} value={due ?? (order.due_at ? order.due_at.slice(0, 10) : '')} onChange={(e) => setDue(e.target.value)} />
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
                <dt className="text-muted-foreground">Balance due</dt>
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
      {current && <ItemDrawer key={current.id} detail={detail} item={current} onClose={() => setOpenItem(null)} onChanged={reload} />}
    </div>
  )
}

function ItemDrawer({ detail, item, onClose, onChanged }: { detail: OrderDetail; item: OrderItemDetail; onClose: () => void; onChanged: () => void }) {
  const { order } = detail
  const editable = OPEN_FOR_EDITS.has(order.status)
  const settled = item.status === 'fulfilled' || item.status === 'cancelled'
  const [error, setError] = useState<string | null>(null)
  const [quantity, setQuantity] = useState(String(item.quantity))
  const [title, setTitle] = useState(item.title)
  useEffect(() => {
    setQuantity(String(item.quantity))
    setTitle(item.title)
  }, [item.quantity, item.title])
  const price = itemPrice(item)

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
          <span className="text-xs text-muted-foreground">
            {item.reference.slug} @ {item.reference.commit.slice(0, 7)}
          </span>
          {item.output_id && (
            <Link to={`/m/${item.reference.slug}/history`} className="ml-auto text-xs text-primary underline-offset-4 hover:underline">
              From an output
            </Link>
          )}
        </>
      }
      footer={
        item.status === 'cancelled' && !ENDED.has(order.status) ? (
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
          const count = Number(quantity)
          if (!Number.isInteger(count) || count < 1) {
            setError('Enter a quantity of 1 or more.')
            return
          }
          void run(() => api.updateOrderItem(order.id, item.id, { ...(count !== item.quantity ? { quantity: count } : {}), ...(title.trim() !== item.title ? { title: title.trim() } : {}) }))
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
        <Button type="submit" size="sm" variant="outline" disabled={settled || ENDED.has(order.status) || (quantity === String(item.quantity) && title === item.title)}>
          Save item
        </Button>
      </form>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[13px]">
        <dt className="text-muted-foreground">Price</dt>
        <dd className="sb-num">
          {price === null ? 'Not priced' : formatMoney(price.amount, order.currency, order.minor_exponent)}
          {price && !price.accepted && <span className="ml-1 text-xs text-muted-foreground">(not accepted)</span>}
        </dd>
        <dt className="text-muted-foreground">Inputs</dt>
        <dd className="break-all font-mono text-xs">{JSON.stringify((item.reference.inputs as { params?: unknown }).params ?? {})}</dd>
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
      <QuoteEditor orderId={order.id} itemId={item.id} currency={order.currency} exponent={order.minor_exponent} quotes={item.quotes} editable={editable && !settled} onChanged={onChanged} item={item} />
      <FulfilmentForm orderId={order.id} itemId={item.id} quantity={item.quantity} attempts={item.fulfilments} enabled={IN_PRODUCTION.has(order.status) && !order.on_hold && !settled} onChanged={onChanged} />
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
