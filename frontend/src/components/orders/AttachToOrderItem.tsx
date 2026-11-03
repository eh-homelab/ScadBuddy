import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { FulfilmentInput, Order, OrderItemDetail } from '../../api/types'
import { DEFAULT_ORDERS_QUERY } from '../../lib/ordersQuery'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from './refusal'

/** Orders asked for at a time. */
const PAGE = 100
/** An accepted order still being made: its items take attempts. */
const IN_WORK = new Set<Order['status']>(['accepted', 'in_production', 'ready'])
/** An item an attempt may be recorded for (`orders/lifecycle.py` `FULFILLABLE`). */
const OPEN_ITEM = new Set<OrderItemDetail['status']>(['approved', 'failed', 'queued', 'printing', 'printed'])

function outcomeOf(status: string): FulfilmentInput['outcome'] {
  if (status === 'completed') return 'succeeded'
  if (status === 'failed' || status === 'cancelled' || status === 'aborted') return 'failed'
  return 'unknown'
}

/**
 * #4251 (spec 2026-10-18 §9) — "Attach to order item" on a print's page: the print, made
 * outside the order's own print path, is recorded as an attempt of a line item naming
 * its Bambuddy archive, whose time, grams, finish photo and timelapse the attempt then
 * copies as a print's would (§8.2).
 */
export function AttachToOrderItem({ open, archiveId, printStatus, onClose }: { open: boolean; archiveId: number; printStatus: string; onClose: () => void }) {
  const outcome = outcomeOf(printStatus)
  // #5273: searched (number, notes, customer) and paged, so no order is out of reach.
  const [search, setSearch] = useState('')
  const [found, setFound] = useState<{ orders: Order[]; next: string | null } | null>(null)
  const [more, setMore] = useState(false)
  const [orderId, setOrderId] = useState('')
  const detail = useAsync(() => (orderId ? api.order(orderId) : Promise.resolve(undefined)), [orderId])
  const [itemId, setItemId] = useState('')
  const [made, setMade] = useState(outcome === 'failed' ? '0' : '1')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<{ order: Order; itemId: string } | null>(null)
  const choices = (found?.orders ?? []).filter((o) => IN_WORK.has(o.status))
  // The order chosen stays offered while a search shows others.
  const chosen = detail.data?.order
  if (chosen && chosen.id === orderId && !choices.some((o) => o.id === chosen.id)) choices.unshift(chosen)
  const items = (detail.data?.items ?? []).filter((i) => i.reference !== null && OPEN_ITEM.has(i.status))

  useEffect(() => {
    if (!open) return
    let live = true
    setFound(null)
    const timer = setTimeout(
      () => {
        api.orders({ ...DEFAULT_ORDERS_QUERY, q: search.trim() }, { limit: PAGE }).then(
          (page) => live && setFound({ orders: page.items, next: page.next_cursor }),
          (err: unknown) => {
            if (!live) return
            setFound({ orders: [], next: null })
            setError(refusalText(err))
          },
        )
      },
      search ? 250 : 0,
    )
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [open, search])

  async function loadMore() {
    const cursor = found?.next
    if (!cursor) return
    setMore(true)
    try {
      const page = await api.orders({ ...DEFAULT_ORDERS_QUERY, q: search.trim() }, { limit: PAGE, cursor })
      // Only onto the list it continues: a search typed meanwhile has its own.
      setFound((prev) => (prev && prev.next === cursor ? { orders: [...prev.orders, ...page.items], next: page.next_cursor } : prev))
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setMore(false)
    }
  }

  useEffect(() => {
    if (!open) {
      setSearch('')
      setFound(null)
      setOrderId('')
      setItemId('')
      setError(null)
      setDone(null)
      setBusy(false)
    }
  }, [open])

  async function attach() {
    const quantity = Number(made)
    if (!Number.isInteger(quantity) || quantity < 0) {
      setError('Enter how many this print made, such as 1.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.recordFulfilment(orderId, itemId, { quantity, outcome, archive_id: archiveId, notes: `Bambuddy archive ${archiveId}` })
      setDone({ order: detail.data!.order, itemId })
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Attach to order item</DialogTitle>
          <DialogDescription>This print becomes an attempt of a line item ({outcome}), with its time, filament, photos and timelapse.</DialogDescription>
        </DialogHeader>
        {done ? (
          <p className="text-[13px]">
            Recorded.{' '}
            <Link className="underline" to={`/orders/${done.order.id}?item=${encodeURIComponent(done.itemId)}`}>
              Open {done.order.number}
            </Link>
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="attach-search">Search orders</Label>
              <Input id="attach-search" type="search" placeholder="Number, customer or notes" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="attach-order">Order</Label>
              <Select
                value={orderId}
                onValueChange={(value) => {
                  setOrderId(value)
                  setItemId('')
                }}
              >
                <SelectTrigger id="attach-order" aria-label="Order">
                  <SelectValue placeholder={found === null ? 'Loading…' : choices.length ? 'Choose an order' : 'No order in production'} />
                </SelectTrigger>
                <SelectContent position="popper">
                  {choices.map((o) => (
                    <SelectItem key={o.id} value={o.id}>
                      {o.number} ({o.status})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {found?.next && (
                <Button type="button" variant="link" size="sm" className="self-start px-0" disabled={more} onClick={() => void loadMore()}>
                  More orders
                </Button>
              )}
            </div>
            {orderId && (
              <div className="flex flex-col gap-1">
                <Label htmlFor="attach-item">Line item</Label>
                <Select value={itemId} onValueChange={setItemId}>
                  <SelectTrigger id="attach-item" aria-label="Line item">
                    <SelectValue placeholder={items.length ? 'Choose a line item' : 'No item open for an attempt'} />
                  </SelectTrigger>
                  <SelectContent position="popper">
                    {items.map((i) => (
                      <SelectItem key={i.id} value={i.id}>
                        {i.title} ({i.status})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="flex flex-col gap-1">
              <Label htmlFor="attach-made">Made</Label>
              <Input id="attach-made" inputMode="numeric" className="w-24" value={made} onChange={(e) => setMade(e.target.value)} />
            </div>
            {error && (
              <p role="alert" className="text-[13px] text-warn">
                {error}
              </p>
            )}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            {done ? 'Close' : 'Cancel'}
          </Button>
          {!done && (
            <Button type="button" disabled={busy || !orderId || !itemId} onClick={() => void attach()}>
              Attach
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
