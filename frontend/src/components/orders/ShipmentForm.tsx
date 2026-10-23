import { useState } from 'react'
import { api } from '../../api/client'
import type { OrderItemDetail, Shipment } from '../../api/types'
import { formatMoney, toMinor } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { refusalText } from './refusal'

/** What each fulfilled item still has to ship, after the shipments so far. */
function remaining(items: OrderItemDetail[], shipments: Shipment[]): { item: OrderItemDetail; left: number }[] {
  const shipped: Record<string, number> = {}
  for (const s of shipments) for (const line of s.items) shipped[line.order_item_id] = (shipped[line.order_item_id] ?? 0) + line.quantity
  return items.filter((i) => i.status === 'fulfilled').map((item) => ({ item, left: Math.max(0, item.quantity - (shipped[item.id] ?? 0)) }))
}

/** Shipments (§6.3): the ones recorded, and a form for the next, carrying fulfilled items. */
export function ShipmentForm({
  orderId,
  currency,
  exponent,
  items,
  shipments,
  enabled,
  onChanged,
}: {
  orderId: string
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  items: OrderItemDetail[]
  shipments: Shipment[]
  enabled: boolean
  onChanged: () => void
}) {
  const left = remaining(items, shipments)
  const [carrier, setCarrier] = useState('')
  const [tracking, setTracking] = useState('')
  const [cost, setCost] = useState('')
  const [quantities, setQuantities] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const titles = Object.fromEntries(items.map((i) => [i.id, i.title]))

  return (
    <section className="flex flex-col gap-2" aria-label="Shipments">
      <h3 className="text-xs text-muted-foreground">Shipments</h3>
      {shipments.length > 0 && (
        <ul className="flex flex-col gap-1 text-[13px]" aria-label="Shipments recorded">
          {shipments.map((s) => (
            <li key={s.id}>
              {[s.carrier, s.service, s.tracking].filter(Boolean).join(' · ') || 'Shipment'}
              {s.cost_minor !== null && s.cost_minor !== undefined ? ` · ${formatMoney(s.cost_minor, currency, exponent)}` : ''}:{' '}
              {s.items.map((line) => `${line.quantity} × ${titles[line.order_item_id] ?? line.order_item_id}`).join(', ') || 'nothing listed'}
            </li>
          ))}
        </ul>
      )}
      {enabled && left.length > 0 && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            const lines = left
              .map(({ item }) => ({ order_item_id: item.id, quantity: Number(quantities[item.id] ?? left.find((l) => l.item.id === item.id)?.left ?? 0) }))
              .filter((line) => Number.isInteger(line.quantity) && line.quantity > 0)
            const costMinor = cost.trim() ? toMinor(cost, exponent) : null
            if (cost.trim() && costMinor === null) {
              setError('Enter the shipping cost as an amount, such as 4.50.')
              return
            }
            setBusy(true)
            setError(null)
            api
              .addShipment(orderId, {
                carrier: carrier.trim() || null,
                tracking: tracking.trim() || null,
                cost_minor: costMinor,
                items: lines,
              })
              .then(() => {
                setCarrier('')
                setTracking('')
                setCost('')
                setQuantities({})
                onChanged()
              })
              .catch((err: unknown) => setError(refusalText(err)))
              .finally(() => setBusy(false))
          }}
        >
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="ship-carrier">Carrier</Label>
              <Input id="ship-carrier" className="w-32" value={carrier} onChange={(e) => setCarrier(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="ship-tracking">Tracking</Label>
              <Input id="ship-tracking" className="w-40" value={tracking} onChange={(e) => setTracking(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="ship-cost">Cost ({currency})</Label>
              <Input id="ship-cost" inputMode="decimal" className="w-24" value={cost} onChange={(e) => setCost(e.target.value)} />
            </div>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            {left.map(({ item, left: count }) => (
              <div key={item.id} className="flex flex-col gap-1">
                <Label htmlFor={`ship-${item.id}`}>{item.title}</Label>
                <Input
                  id={`ship-${item.id}`}
                  inputMode="numeric"
                  className="w-20"
                  value={quantities[item.id] ?? String(count)}
                  onChange={(e) => setQuantities((was) => ({ ...was, [item.id]: e.target.value }))}
                />
              </div>
            ))}
            <Button type="submit" size="sm" disabled={busy}>
              Add shipment
            </Button>
          </div>
        </form>
      )}
      {enabled && left.length === 0 && shipments.length === 0 && <p className="text-[13px] text-muted-foreground">Nothing fulfilled to ship yet.</p>}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
