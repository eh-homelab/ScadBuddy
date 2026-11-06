import type { UnitCostHistory } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { Button } from '@/components/ui/button'

/** How the warning names the cost part that most exceeded what the past quotes priced. */
const REASONS: Record<NonNullable<UnitCostHistory['main_reason']>, string> = {
  filament: 'filament over what the quotes priced',
  machine: 'machine time over what the quotes priced',
  labour: 'labour over what the quotes priced',
  failed_prints: 'failed prints',
  test_prints: 'test prints',
  other: 'other costs',
}

/**
 * Whether `unitPrice` leaves a margin below the thin threshold against the history's real
 * cost per unit: the backend's `orders/history.py` `warns`, on the unrounded percent. False
 * with no history.
 */
function isBelowCost(history: UnitCostHistory | null | undefined, unitPrice: number): boolean {
  const cost = history?.unit_cost_minor
  if (cost == null || !history || history.jobs === 0) return false
  if (unitPrice <= 0) return cost > 0
  return ((unitPrice - cost) * 100) / unitPrice < Number(history.thin_margin_percent)
}

/**
 * The quote warning (job cost spec 2026-11-05 §6.3, #5328): the price is below what this
 * model really cost in its last fulfilled jobs. Advice only: it never blocks the quote.
 * Nothing shows without history or when the price is not thin.
 */
export function QuoteWarning({
  history,
  unitPrice,
  currency,
  exponent,
  onUse,
}: {
  history: UnitCostHistory | null | undefined
  unitPrice: number
  currency: string
  exponent: number
  /** Puts the suggested unit price into the price field. */
  onUse: (minor: number) => void
}) {
  if (!history || !isBelowCost(history, unitPrice) || history.unit_cost_minor == null) return null
  const money = (minor: number) => formatMoney(minor, currency, exponent)
  const suggested = history.suggested_unit_price_minor
  return (
    <div role="alert" className="flex flex-col gap-1 rounded-md border border-warn/40 p-2 text-[13px]" data-testid="quote-warning">
      <p className="text-warn">
        {money(unitPrice)} a unit is below this model&apos;s real cost: {history.estimated ? 'about ' : ''}
        <span className="sb-num">{money(history.unit_cost_minor)}</span> a unit over its last {history.jobs}{' '}
        {history.jobs === 1 ? 'job' : 'jobs'}.
      </p>
      {history.main_reason && history.main_reason_minor != null && (
        <p className="text-muted-foreground">
          Main reason: {REASONS[history.main_reason]}, <span className="sb-num">{money(history.main_reason_minor)}</span> a unit.
        </p>
      )}
      {suggested != null && (
        <p className="flex flex-wrap items-center gap-2 text-muted-foreground">
          Suggested: <span className="sb-num text-foreground">{money(suggested)}</span> a unit
          {Number(history.markup_percent) > 0 ? ` (${history.markup_percent} % markup)` : ` (a ${history.thin_margin_percent} % margin)`}.
          <Button type="button" size="xs" variant="outline" onClick={() => onUse(suggested)}>
            Use {money(suggested)}
          </Button>
        </p>
      )}
    </div>
  )
}
