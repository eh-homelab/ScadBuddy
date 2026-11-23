import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import { calculateCostPerKg, type CostCalculation } from '../../api/filament'
import { cmpDecimals, parseDecimal } from '../../lib/decimal'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useMoney } from './purchasing/useMoney'

/** `purchasing_models.Money`: at most four places. */
const MONEY = /^\d{1,8}(?:\.\d{1,4})?$/

export interface CostCalculatorProps {
  /** The type's label weight as typed: the net weight starts from it while that field is untouched. */
  labelWeight: string
  /** Puts the cost per kg in the type's cost field as an unsaved edit; Save writes it (R-7152-4). */
  onUseAsCost: (costPerKg: string) => void
}

interface Fields {
  price: string
  quantity: string
  tax: string
  shipping: string
  weight: string
}

/** Typed text as the route's money, `''` as `fallback`; `null` when it is not an amount. */
function money(text: string, fallback: string | null): string | null {
  if (text.trim() === '') return fallback
  const value = parseDecimal(text)
  return value != null && MONEY.test(value) ? value : null
}

function whole(text: string, max: number): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const value = Number(text.trim())
  return value >= 1 && value <= max ? value : null
}

/**
 * #7152 — the catalog's cost calculator: what a spool's price, its tax and shipping and its net
 * weight come to per kg. The sum is the server's (`GET /filament/cost-per-kg`, exact decimals in
 * the shop's currency, R-7152-1 to 3); "Use as default cost" only fills the type's cost field.
 */
export function CostCalculator({ labelWeight, onUseAsCost }: CostCalculatorProps) {
  const shop = useMoney()
  const [open, setOpen] = useState(false)
  const [fields, setFields] = useState<Fields>({ price: '', quantity: '1', tax: '', shipping: '', weight: labelWeight })
  const [weightEdited, setWeightEdited] = useState(false)
  const [result, setResult] = useState<CostCalculation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const latest = useRef(0)

  useEffect(() => {
    if (!weightEdited) setFields((now) => ({ ...now, weight: labelWeight }))
  }, [labelWeight, weightEdited])

  const set = (key: keyof Fields, value: string) => {
    setFields((now) => ({ ...now, [key]: value }))
    if (key === 'weight') setWeightEdited(true)
    setResult(null)
  }

  const work = async () => {
    const price = money(fields.price, null)
    const tax = money(fields.tax, '0')
    const shipping = money(fields.shipping, '0')
    const quantity = whole(fields.quantity, 1000)
    const weight = whole(fields.weight, 100_000)
    const refusal =
      price == null
        ? 'Enter the price paid, such as 19.99.'
        : tax == null || shipping == null
          ? 'Enter the tax and shipping as amounts with at most four decimals, or leave them empty.'
          : quantity == null
            ? 'Enter the number of spools as a whole number from 1 to 1000.'
            : weight == null
              ? 'Enter the net weight of one spool in grams, from 1 to 100000.'
              : null
    if (refusal) {
      setError(refusal)
      setResult(null)
      return
    }
    const mine = ++latest.current
    setBusy(true)
    try {
      const found = await calculateCostPerKg({ price: price!, tax: tax!, shipping: shipping!, quantity: quantity!, net_weight_g: weight! })
      if (mine !== latest.current) return
      setResult(found)
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(cause instanceof ApiError ? cause.detail : 'The cost could not be worked out. Try again.')
    } finally {
      if (mine === latest.current) setBusy(false)
    }
  }

  if (!open) {
    return (
      <div>
        <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
          Work out the cost per kg
        </Button>
      </div>
    )
  }

  const field = (key: keyof Fields, label: string, mode: 'decimal' | 'numeric', hint?: string) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`cost-${key}`} className="text-[12px]">
        {label}
      </Label>
      <Input id={`cost-${key}`} inputMode={mode} value={fields[key]} onChange={(e) => set(key, e.target.value)} />
      {hint && <p className="text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  )

  return (
    <section aria-label="Cost calculator" className="flex flex-col gap-3 rounded-md border bg-muted/30 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-[13px] font-medium">Cost per kg from a purchase</h3>
        <button type="button" className="text-[12px] text-muted-foreground underline-offset-4 hover:underline" onClick={() => setOpen(false)}>
          Close
        </button>
      </div>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          void work()
        }}
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {field('price', `Price paid${shop.currency ? ` (${shop.currency})` : ''}`, 'decimal')}
          {field('quantity', 'Spools in the price', 'numeric')}
          {field('weight', 'Net weight per spool (g)', 'numeric', 'Filament only, without the spool.')}
          {field('tax', 'Tax', 'decimal', 'Empty is none.')}
          {field('shipping', 'Shipping', 'decimal', 'Empty is none.')}
        </div>
        <div>
          <Button type="submit" size="sm" disabled={busy}>
            {busy ? 'Working it out…' : 'Work it out'}
          </Button>
        </div>
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {result && (
        <div aria-live="polite" className="flex flex-wrap items-end justify-between gap-3 border-t pt-3">
          <div className="min-w-0">
            <p className="text-xl font-semibold tabular-nums tracking-tight" data-testid="cost-per-kg">
              {shop.format(result.cost_per_kg, result.currency)}
              <span className="ml-1 text-[13px] font-normal text-muted-foreground">per kg</span>
            </p>
            <p className="text-[12px] text-muted-foreground">
              {shop.format(result.total, result.currency)} for {result.quantity} {result.quantity === 1 ? 'spool' : 'spools'} of {result.net_weight_g} g, tax and shipping in.
              {cmpDecimals(result.shipping, '0') !== 0 && ` Without shipping, ${shop.format(result.price_per_kg, result.currency)} per kg, as a supplier price is kept.`}
            </p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={() => onUseAsCost(result.cost_per_kg)}>
            Use as default cost
          </Button>
        </div>
      )}
    </section>
  )
}
