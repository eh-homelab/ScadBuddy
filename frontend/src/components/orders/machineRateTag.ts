import type { AttemptCost, MachineRateBreakdown } from '../../api/types'

/**
 * #6665 (machine-rate actuals spec 2026-11-17 §6) — the short tag beside a machine figure:
 * "actuals" or "estimate", with the confidence when there is one ("estimate, 33 % confidence").
 */
export function rateTag(actuals: boolean, confidence: number | null | undefined): string {
  return `${actuals ? 'actuals' : 'estimate'}${confidence == null ? '' : `, ${confidence} % confidence`}`
}

type Basis = NonNullable<AttemptCost['machine_rate_basis']>

const SOURCE: Record<Basis, string> = {
  quote_actuals: 'From the quote',
  quote_estimate: 'From the quote',
  printer_actuals: 'Today’s rate for the printer',
  printer_estimate: 'Today’s rate for the printer',
  price_book: 'The price book’s machine rate',
}

/** An attempt's tag and its tooltip (where the rate came from); null with no machine figure. */
export function attemptTag(basis: AttemptCost['machine_rate_basis'], confidence: AttemptCost['machine_rate_confidence']): { text: string; title: string } | null {
  if (!basis) return null
  return { text: rateTag(basis === 'quote_actuals' || basis === 'printer_actuals', confidence), title: SOURCE[basis] }
}

/** The order's line when some attempt's machine time is priced from actuals. */
export function basesNote(bases: AttemptCost['machine_rate_basis'][] | undefined): string | null {
  const list = bases ?? []
  const actual = list.filter((b) => b === 'quote_actuals' || b === 'printer_actuals').length
  if (actual === 0) return null
  return actual === list.length ? 'Machine time priced from actuals.' : 'Machine time partly priced from actuals.'
}

/** The row set to its actual with the least logged history: "Nozzle: 1 of 3 replacements logged". */
export function leastBacked(rate: MachineRateBreakdown): string | null {
  const rows = [
    ...(rate.wear ?? []).filter((w) => w.priced_by === 'actual').map((w) => ({ name: w.name, have: w.have, need: w.need, confidence: w.confidence, unit: 'replacements' })),
    ...(rate.maintenance ?? []).filter((m) => m.priced_by === 'actual' && m.counted).map((m) => ({ name: m.task, have: m.have, need: m.need, confidence: m.confidence, unit: 'gaps' })),
  ]
  if (rows.length === 0) return null
  // Least backed: the lowest confidence, then the smallest share of its window logged.
  const share = (row: { have: number; need?: number | null }) => (row.need ? row.have / row.need : 1)
  const least = rows.reduce((low, row) => {
    const a = row.confidence ?? 0
    const b = low.confidence ?? 0
    return a < b || (a === b && share(row) < share(low)) ? row : low
  })
  return `${least.name}: ${Math.min(least.have, least.need ?? least.have)} of ${least.need ?? rate.actuals_window ?? least.have} ${least.unit} logged`
}
