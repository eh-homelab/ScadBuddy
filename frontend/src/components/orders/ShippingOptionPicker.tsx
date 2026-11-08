import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { Order, OrderCharge, ShippingOption } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { isCart, OPEN_FOR_EDITS } from './afterAcceptance'
import { refusalText } from './refusal'

const NONE = '__none__'

/**
 * #5281 (storefront spec §8) — the order's shipping option, from Settings → Orders'
 * `storefront_shipping_options`: its one `shipping` charge at the option's label and price.
 * Chosen while draft or quoted; an option that ships needs the order's shipping address, and
 * the backend's refusal (`shipping_address_required`) is shown as it says it. A pickup needs
 * none.
 */
export function ShippingOptionPicker({
  order,
  charges,
  options,
  onChanged,
  disabled = false,
  onBusyChange,
}: {
  order: Pick<Order, 'id' | 'status' | 'currency' | 'minor_exponent' | 'source' | 'submitted_at'>
  charges: OrderCharge[]
  options: ShippingOption[]
  onChanged: () => void
  /** While the charges editor has unsaved rows: a save of them would undo the option. */
  disabled?: boolean
  /** Told when a write starts and once its reload has brought the new charges, so the
   * charges editor starts no edit on rows the write replaces. */
  onBusyChange?: (busy: boolean) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A write answered: the charges it was made over, until the reload brings new ones.
  const writtenOver = useRef<OrderCharge[] | null>(null)
  useEffect(() => {
    if (writtenOver.current !== null && charges !== writtenOver.current) {
      writtenOver.current = null
      setBusy(false)
    }
  }, [charges])
  const told = useRef(onBusyChange)
  useEffect(() => {
    told.current = onBusyChange
  })
  useEffect(() => {
    told.current?.(busy)
  }, [busy])
  const chosen = charges.find((c) => c.kind === 'shipping' && c.shipping_option_id)?.shipping_option_id ?? null
  const editable = OPEN_FOR_EDITS.has(order.status) && !isCart(order)
  if (options.length === 0 && chosen === null) return null
  const text = (option: ShippingOption) => `${option.label} · ${option.ships ? formatMoney(option.amount_minor, order.currency, order.minor_exponent) : 'pickup, no address'}`
  const current = options.find((o) => o.id === chosen)

  if (!editable) {
    return (
      <div className="flex flex-col gap-1 text-[13px]">
        <span className="text-xs text-muted-foreground">Shipping option</span>
        <span>{current ? text(current) : chosen ?? 'None.'}</span>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-1 text-[13px]">
      <span className="text-xs text-muted-foreground">Shipping option</span>
      <Select
        value={chosen ?? NONE}
        disabled={busy || disabled}
        onValueChange={(value) => {
          setBusy(true)
          setError(null)
          api
            .setShippingOption(order.id, value === NONE ? null : value)
            .then(() => {
              writtenOver.current = charges
              onChanged()
            })
            .catch((err: unknown) => {
              setError(refusalText(err))
              setBusy(false)
            })
        }}
      >
        <SelectTrigger aria-label="Shipping option" className="w-full">
          <SelectValue placeholder="No shipping option" />
        </SelectTrigger>
        <SelectContent position="popper">
          <SelectItem value={NONE}>No shipping option</SelectItem>
          {chosen !== null && !current && <SelectItem value={chosen}>{`${chosen} (removed)`}</SelectItem>}
          {options.map((option) => (
            <SelectItem key={option.id} value={option.id}>
              {text(option)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {disabled && <p className="text-xs text-muted-foreground">Save or discard the charges first</p>}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
