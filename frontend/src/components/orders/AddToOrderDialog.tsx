import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { api } from '../../api/client'
import type { Customer, Order } from '../../api/types'
import { DEFAULT_ORDERS_QUERY } from '../../lib/ordersQuery'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from './refusal'

const NEW = '__new__'

/**
 * "Add to order…" (spec 2026-10-18 §3.3, §9): an output becomes a line item of an
 * order. Pick or make the customer, pick one of their open (draft or quoted) orders or
 * make one, say how many, and the page moves to the order.
 */
export function AddToOrderDialog({ open, outputId, outputName, onClose }: { open: boolean; outputId: string; outputName?: string | null; onClose: () => void }) {
  const navigate = useNavigate()
  const customers = useAsync(() => (open ? api.customers() : Promise.resolve([] as Customer[])), [open])
  const [customerId, setCustomerId] = useState('')
  const [customerName, setCustomerName] = useState('')
  const [orderId, setOrderId] = useState(NEW)
  const [quantity, setQuantity] = useState('1')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const chosen = customerId && customerId !== NEW ? customerId : ''
  const orders = useAsync(
    () => (chosen ? api.orders({ ...DEFAULT_ORDERS_QUERY, customer: chosen }, { limit: 100 }) : Promise.resolve({ items: [] as Order[], next_cursor: null })),
    [chosen],
  )
  const openOrders = (orders.data?.items ?? []).filter((o) => o.status === 'draft' || o.status === 'quoted')
  useEffect(() => {
    if (!open) {
      setError(null)
      setBusy(false)
    }
  }, [open])
  useEffect(() => {
    // The newest open order of the customer, else a new one.
    setOrderId(openOrders[0]?.id ?? NEW)
  }, [chosen, orders.data]) // eslint-disable-line react-hooks/exhaustive-deps

  async function add() {
    const count = Number(quantity)
    if (!Number.isInteger(count) || count < 1) {
      setError('Enter how many to make, 1 or more.')
      return
    }
    if (!customerId || (customerId === NEW && !customerName.trim())) {
      setError('Pick a customer, or name a new one.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const customer = customerId === NEW ? (await api.createCustomer({ display_name: customerName.trim() })).id : customerId
      const order = orderId === NEW ? (await api.createOrder({ customer_id: customer })).id : orderId
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
          <div className="flex flex-col gap-1">
            <Label htmlFor="order-customer">Customer</Label>
            <Select value={customerId} onValueChange={setCustomerId}>
              <SelectTrigger id="order-customer" aria-label="Customer">
                <SelectValue placeholder={customers.loading ? 'Loading…' : 'Pick a customer'} />
              </SelectTrigger>
              <SelectContent position="popper">
                {(customers.data ?? []).map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.display_name}
                  </SelectItem>
                ))}
                <SelectItem value={NEW}>New customer…</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {customerId === NEW && (
            <div className="flex flex-col gap-1">
              <Label htmlFor="order-customer-name">Customer name</Label>
              <Input id="order-customer-name" value={customerName} onChange={(e) => setCustomerName(e.target.value)} />
            </div>
          )}
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
