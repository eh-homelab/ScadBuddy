import { useState } from 'react'
import { api } from '../../api/client'
import type { Order, OrderItemDetail, Shipment } from '../../api/types'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Spinner } from '@/components/ui/spinner'
import { itemActions, type ItemActionKey } from './itemActions'
import { ItemPrintDialog } from './PrintItem'
import { parseQuantity, QUANTITY_HINT } from './quantity'
import { refusalItems, refusalText } from './refusal'

/** What the selected items can do now: each action's items, by the ⋯ menu's rules. */
function allowed(order: Order, items: OrderItemDetail[], rules: { shipments: Shipment[]; unfulfilAllowed: boolean }, keys: ItemActionKey[]) {
  return items.filter((item) => itemActions(order, item, rules).some((action) => keys.includes(action.key) && action.blocked === null))
}

/** The statuses the bulk fulfil takes (`lifecycle.py` `FULFILLABLE`): it refuses any other
 * item, and then writes nothing for the rest. */
const FULFILLABLE = new Set(['approved', 'failed'])

/** How many of the item are still to make: its quantity less its accepted production attempts. */
function remaining(item: OrderItemDetail): number {
  const made = item.fulfilments.filter((f) => f.purpose === 'production' && f.accepted_at != null).reduce((sum, f) => sum + f.quantity, 0)
  return Math.max(1, item.quantity - made)
}

/**
 * #4955 step 3, spec 2026-11-07 §4.1 — the items card's bulk actions on the selected rows:
 * Fulfil (one dialog, a quantity per item, `POST /orders/{id}/fulfil`), Print (each item's
 * print dialog in turn) and Remove (after a confirm: a draft is deleted, the rest cancelled).
 * Each acts on the selected items its ⋯ menu allows it on now.
 */
export function ItemsBulk({
  order,
  selected,
  shipments,
  unfulfilAllowed,
  busy,
  onWriting,
  onClear,
  onChanged,
}: {
  order: Order
  selected: OrderItemDetail[]
  shipments: Shipment[]
  unfulfilAllowed: boolean
  busy: boolean
  /** A bulk write starts (true) or ends (false): the page's other writes wait for it. */
  onWriting: (writing: boolean) => void
  onClear: () => void
  onChanged: () => void
}) {
  const rules = { shipments, unfulfilAllowed }
  const fulfil = allowed(order, selected, rules, ['fulfil']).filter((item) => FULFILLABLE.has(item.status))
  const print = allowed(order, selected, rules, ['print'])
  const remove = allowed(order, selected, rules, ['delete', 'cancel'])
  const [fulfilling, setFulfilling] = useState(false)
  const [printing, setPrinting] = useState<OrderItemDetail[]>([])
  const [removing, setRemoving] = useState(false)
  return (
    <div className="flex w-full flex-wrap items-center gap-2 rounded-md bg-muted/50 px-2 py-1 text-[13px]" data-testid="bulk-actions">
      <span className="mr-1 font-medium">{selected.length} selected</span>
      <Button type="button" size="sm" variant="outline" disabled={busy || fulfil.length === 0} onClick={() => setFulfilling(true)}>
        Fulfil…
      </Button>
      <Button type="button" size="sm" variant="outline" disabled={busy || print.length === 0} onClick={() => setPrinting(print)}>
        Print…
      </Button>
      <Button type="button" size="sm" variant="outline" disabled={busy || remove.length === 0} onClick={() => setRemoving(true)}>
        Remove…
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={onClear}>
        Clear selection
      </Button>
      {fulfil.length === 0 && print.length === 0 && <span className="text-xs text-muted-foreground">No selected item can be fulfilled or printed now.</span>}
      {fulfilling && (
        <FulfilDialog
          orderId={order.id}
          items={fulfil}
          onWriting={onWriting}
          onClose={() => setFulfilling(false)}
          onDone={() => {
            setFulfilling(false)
            onClear()
            onChanged()
          }}
        />
      )}
      {printing[0] && (
        <ItemPrintDialog
          key={printing[0].id}
          order={order}
          item={printing[0]}
          onClose={() => setPrinting((queue) => queue.slice(1))}
          onChanged={onChanged}
        />
      )}
      <RemoveDialog
        orderId={order.id}
        items={remove}
        onWriting={onWriting}
        open={removing}
        onOpenChange={setRemoving}
        onDone={() => {
          setRemoving(false)
          onClear()
          onChanged()
        }}
        onChanged={onChanged}
      />
    </div>
  )
}

/** One dialog for the selected items' fulfilment: how many of each were made, all or none. */
function FulfilDialog({
  orderId,
  items,
  onWriting,
  onClose,
  onDone,
}: {
  orderId: string
  items: OrderItemDetail[]
  onWriting: (writing: boolean) => void
  onClose: () => void
  onDone: () => void
}) {
  const [typed, setTyped] = useState<Record<string, string>>(() => Object.fromEntries(items.map((item) => [item.id, String(remaining(item))])))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const titles = new Map(items.map((item) => [item.id, item.title]))

  async function submit() {
    const lines = items.map((item) => ({ item_id: item.id, quantity: parseQuantity(typed[item.id] ?? String(remaining(item))) }))
    if (lines.some((line) => line.quantity === null)) return setError(QUANTITY_HINT)
    setBusy(true)
    onWriting(true)
    setError(null)
    try {
      await api.fulfilOrderItems(orderId, lines.map((line) => ({ item_id: line.item_id, quantity: line.quantity! })))
      onDone()
    } catch (err) {
      const named = refusalItems(err).map((id) => titles.get(id) ?? id)
      setError(named.length > 0 ? `${refusalText(err)} (${named.join(', ')})` : refusalText(err))
    } finally {
      setBusy(false)
      onWriting(false)
    }
  }

  return (
    <Dialog open onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Fulfil items</DialogTitle>
          <DialogDescription>How many of each item were made. Each is recorded as an accepted attempt by hand; an item is fulfilled once its accepted attempts reach its quantity.</DialogDescription>
        </DialogHeader>
        <form
          id="bulk-fulfil"
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!busy) void submit()
          }}
        >
          {items.map((item) => (
            <label key={item.id} className="flex items-center justify-between gap-2 text-sm">
              <span className="break-words">
                {item.title} <span className="text-xs text-muted-foreground">of {item.quantity}</span>
              </span>
              <Input
                aria-label={`Quantity made of ${item.title}`}
                inputMode="numeric"
                className="h-8 w-20 text-right"
                // An item that joins on a reload starts at what is still to make.
                value={typed[item.id] ?? String(remaining(item))}
                disabled={busy}
                onChange={(event) => {
                  setTyped({ ...typed, [item.id]: event.target.value })
                  setError(null)
                }}
              />
            </label>
          ))}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </form>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="bulk-fulfil" disabled={busy}>
            {busy && <Spinner />}
            Fulfil
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The confirm before the selected items go: a draft is deleted, the rest cancelled, one by one.
 * A refusal stops it there and shows; the items removed before it stay removed. */
function RemoveDialog({
  orderId,
  items,
  onWriting,
  open,
  onOpenChange,
  onDone,
  onChanged,
}: {
  orderId: string
  items: OrderItemDetail[]
  onWriting: (writing: boolean) => void
  open: boolean
  onOpenChange: (open: boolean) => void
  onDone: () => void
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirm() {
    setBusy(true)
    onWriting(true)
    setError(null)
    let removed = 0
    try {
      for (const item of items) {
        if (item.status === 'draft') await api.deleteOrderItem(orderId, item.id)
        else await api.cancelOrderItem(orderId, item.id)
        removed += 1
      }
      onDone()
    } catch (err) {
      setError(`${items[removed]?.title ?? 'An item'}: ${refusalText(err)}`)
      if (removed > 0) onChanged()
    } finally {
      setBusy(false)
      onWriting(false)
    }
  }

  const noun = items.length === 1 ? 'item' : 'items'
  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return
        if (!next) setError(null)
        onOpenChange(next)
      }}
    >
      <AlertDialogContent size="sm">
        <AlertDialogHeader>
          <AlertDialogTitle>{`Remove ${items.length} ${noun}?`}</AlertDialogTitle>
          <AlertDialogDescription>A draft item is deleted. Any other item is cancelled: it stays on the order, in no sum, and can be reopened.</AlertDialogDescription>
        </AlertDialogHeader>
        <ul className="flex flex-col gap-0.5 text-sm">
          {items.map((item) => (
            <li key={item.id}>{`${item.status === 'draft' ? 'Delete' : 'Cancel'} ${item.title}`}</li>
          ))}
        </ul>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Keep</AlertDialogCancel>
          <Button variant="destructive" disabled={busy} onClick={() => void confirm()}>
            {busy && <Spinner />}
            Remove
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
