import { useId, useState, type FormEvent } from 'react'
import { ApiError } from '../../../api/client'
import { addPrice, type PricePoint } from '../../../api/filamentPurchasing'
import { cmpDecimals, parseDecimal } from '../../../lib/decimal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

import { today } from './priceFormat'

export interface AddPriceFormProps {
  productId: string
  /** The currency the price is in; shown, never typed. */
  currency: string | null
  onAdded: (point: PricePoint) => void
  /** Names the product in the field labels, so a page of many forms stays unambiguous. */
  label?: string
}

/**
 * #6266 F18 — a manual price point for one supplier product: the price of a pack with tax in,
 * shipping out, and the day it was seen. The Suppliers page and the Types page share it. The
 * price goes to the API as the typed decimal string, never as a number.
 */
export function AddPriceForm({ productId, currency, onAdded, label }: AddPriceFormProps) {
  const id = useId()
  const [price, setPrice] = useState('')
  const [onDate, setOnDate] = useState(() => today())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const suffix = label ? ` for ${label}` : ''

  async function submit(event: FormEvent) {
    event.preventDefault()
    const parsed = parseDecimal(price)
    if (parsed === null || cmpDecimals(parsed, '0') <= 0) {
      setError('Enter the price as a number above zero, such as 21.40.')
      return
    }
    if (!onDate) {
      setError('Pick the date of the price.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const point = await addPrice(productId, { price: parsed, on_date: onDate })
      setPrice('')
      onAdded(point)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The price could not be added. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-wrap items-end gap-2" aria-label={`Add price${suffix}`}>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-price`} className="text-[12px] text-muted-foreground">
          Price with tax{suffix}
        </label>
        <div className="flex items-center gap-1.5">
          <Input
            id={`${id}-price`}
            inputMode="decimal"
            className="h-8 w-28 text-right tabular-nums"
            placeholder="0.00"
            value={price}
            onChange={(event) => {
              setPrice(event.target.value)
              setError(null)
            }}
          />
          <span className="text-[13px] text-muted-foreground" aria-label="Currency">
            {currency ?? ''}
          </span>
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-date`} className="text-[12px] text-muted-foreground">
          Date{suffix}
        </label>
        <Input id={`${id}-date`} type="date" className="h-8 w-40" value={onDate} onChange={(event) => setOnDate(event.target.value)} />
      </div>
      <Button type="submit" size="sm" disabled={busy}>
        Add price
      </Button>
      {error && (
        <p role="alert" className="basis-full text-[12px] text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
