import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { api } from '../../api/client'
import type { Customer, Order, OrderEstimate, PriceBook } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { DEFAULT_ORDERS_QUERY } from '../../lib/ordersQuery'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CustomerPicker } from './CustomerPicker'
import { useCustomerPicker } from './useCustomerPicker'
import { filamentLabel } from './FilamentsEditor'
import { refusalText } from './refusal'

const NEW = '__new__'

/**
 * "Add to order…" (spec 2026-10-18 §3.3, §9): an output becomes a line item of an
 * order. Pick or make the customer, pick one of their open (draft or quoted) orders or
 * make one, say how many, and the page moves to the order. Picking a price-book filament
 * shows a rough estimate of the output's filament (§5.3, #4250): never a quote, and
 * nothing is recorded.
 */
export function AddToOrderDialog({ open, outputId, outputName, onClose }: { open: boolean; outputId: string; outputName?: string | null; onClose: () => void }) {
  const navigate = useNavigate()
  const customers = useAsync(() => (open ? api.customers() : Promise.resolve([] as Customer[])), [open])
  const picker = useCustomerPicker(customers.data)
  const resetPicker = picker.reset
  const [orderId, setOrderId] = useState(NEW)
  const [quantity, setQuantity] = useState('1')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filamentId, setFilamentId] = useState('')
  const book = useAsync(() => (open ? api.priceBook() : Promise.resolve(null as PriceBook | null)), [open])
  const estimate = useAsync(
    () => (open && filamentId ? api.orderEstimate(outputId, filamentId) : Promise.resolve(null as OrderEstimate | null)),
    [open, outputId, filamentId],
  )
  const chosen = picker.chosen
  const orders = useAsync(
    () => (chosen ? api.orders({ ...DEFAULT_ORDERS_QUERY, customer: chosen }, { limit: 100 }) : Promise.resolve({ items: [] as Order[], next_cursor: null })),
    [chosen],
  )
  // An order made here, offered (first, as the newest) until the reload lists it.
  const [made, setMade] = useState<Order | null>(null)
  const openOrders = useMemo(() => {
    const listed = (orders.data?.items ?? []).filter((o) => o.status === 'draft' || o.status === 'quoted')
    return made && made.customer_id === chosen && !listed.some((o) => o.id === made.id) ? [made, ...listed] : listed
  }, [orders.data, made, chosen])
  useEffect(() => {
    if (!open) {
      setError(null)
      setBusy(false)
      // Opened again, it starts empty, as New order does.
      resetPicker()
    }
  }, [open, resetPicker])
  useEffect(() => {
    // The newest open order of the customer, else a new one; an order still offered stays picked.
    setOrderId((was) => (was !== NEW && openOrders.some((o) => o.id === was) ? was : (openOrders[0]?.id ?? NEW)))
  }, [chosen, openOrders])

  async function add() {
    const count = Number(quantity)
    if (!Number.isInteger(count) || count < 1) {
      setError('Enter how many to make, 1 or more.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      // A customer or an order made here stays picked when a later step fails, so a
      // retry reuses it rather than making a second.
      const customer = await picker.resolve()
      let order = orderId
      if (order === NEW) {
        const created = await api.createOrder({ customer_id: customer })
        setMade(created)
        setOrderId(created.id)
        order = created.id
      }
      await api.addOrderItem(order, { output_id: outputId, quantity: count })
      onClose()
      void navigate(`/orders/${order}`)
    } catch (err) {
      setError(refusalText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add to order</DialogTitle>
          <DialogDescription>{outputName ? `${outputName} becomes a line item` : 'This output becomes a line item'} of an order, pinned to its template revision and inputs.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <CustomerPicker picker={picker} id="order-customer" label="Customer" loading={customers.loading} disabled={busy} />
          {chosen && (
            <div className="flex flex-col gap-1">
              <Label htmlFor="order-pick">Order</Label>
              <Select value={orderId} onValueChange={setOrderId}>
                <SelectTrigger id="order-pick" aria-label="Order">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper">
                  {openOrders.map((o) => (
                    <SelectItem key={o.id} value={o.id}>
                      {o.number} ({o.status})
                    </SelectItem>
                  ))}
                  <SelectItem value={NEW}>New order</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}
          <div className="flex flex-col gap-1">
            <Label htmlFor="order-quantity">Quantity</Label>
            <Input id="order-quantity" inputMode="numeric" className="w-24" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
          </div>
          {(book.data?.filaments ?? []).length > 0 && (
            <div className="flex flex-col gap-1">
              <Label htmlFor="order-estimate-filament">Estimate with</Label>
              <Select value={filamentId} onValueChange={setFilamentId}>
                <SelectTrigger id="order-estimate-filament" aria-label="Estimate with">
                  <SelectValue placeholder="Pick a filament for a rough estimate" />
                </SelectTrigger>
                <SelectContent position="popper">
                  {(book.data?.filaments ?? []).map((f) => (
                    <SelectItem key={f.id} value={f.id}>
                      {filamentLabel(f)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {estimate.data && (
                <p className="text-[13px] text-muted-foreground" data-testid="order-estimate">
                  An estimate: about <span className="sb-num text-foreground">{estimate.data.grams} g</span>, filament{' '}
                  <span className="sb-num text-foreground">{formatMoney(estimate.data.filament_minor, estimate.data.currency, estimate.data.minor_exponent)}</span>
                  {estimate.data.unknown_parts > 0 ? `, leaving out ${estimate.data.unknown_parts} part(s) with no volume` : ''}. Quote it from a slice once it is on an order.
                </p>
              )}
              {estimate.error && (
                <p className="text-[13px] text-muted-foreground" data-testid="order-estimate">
                  No estimate: {refusalText(estimate.error)}
                </p>
              )}
            </div>
          )}
          {(error ?? customers.error?.message) && (
            <p role="alert" className="text-[13px] text-warn">
              {error ?? customers.error?.message}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" disabled={busy} onClick={() => void add()}>
            Add to order
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
