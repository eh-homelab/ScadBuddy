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
 * The quote warning (job cost spec 2026-11-05 §6.3, #5328): the price is below what this
 * model really cost in its last fulfilled jobs, or above it with a thin margin. Each case
 * has its own words. Advice only: it never blocks the quote.
 *
 * `history` is the route's answer for a `unit_price_minor`; its `warning` and `below_cost`
 * decide (`orders/history.py` `warns` is the one copy of the thin rule). Nothing shows
 * without a warning, or while the answer is for a price other than `unitPrice` (the field
 * changed and the next read has not landed).
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
  if (!history?.warning || history.unit_price_minor !== unitPrice || history.unit_cost_minor == null) return null
  const money = (minor: number) => formatMoney(minor, currency, exponent)
  const suggested = history.suggested_unit_price_minor
  const cost = (
    <>
      {history.estimated ? 'about ' : ''}
      <span className="sb-num">{money(history.unit_cost_minor)}</span> a unit over its last {history.jobs} {history.jobs === 1 ? 'job' : 'jobs'}
    </>
  )
  return (
    <div role="alert" className="flex flex-col gap-1 rounded-md border border-warn/40 p-2 text-[13px]" data-testid="quote-warning">
      <p className="text-warn">
        {history.below_cost ? (
          <>
            {money(unitPrice)} a unit is below this model&apos;s real cost: {cost}.
          </>
        ) : (
          <>
            {money(unitPrice)} a unit leaves a thin margin, below {history.thin_margin_percent} %, over this model&apos;s real cost: {cost}.
          </>
        )}
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
