/**
 * #7685 (spec §5.6) — the likely cause of a weighing's drift, from what the page already
 * knows: the start gap of an added-full spool (`startGapG`, against the anchor the weighing
 * was compared with, `previousAnchor`, the ledger's anchor rule over the history's events), the grams the ledger took off since that anchor
 * (`gramsSinceAnchor`), the density the prints imply against the type's (`SpoolMetrics`),
 * and where the empty weight came from. A sentence, never a booking. The first rule that
 * fits wins, in this order:
 *
 * 1. `start_amount`: the scale found the label weight: the drift is the gap between the
 *    label and the starting amount the ledger anchored on (a Bambuddy pull's figure).
 * 2. `density`: the ledger took the slicer's grams, sliced at the density the prints imply
 *    (the slicer's own grams over its own metres, so its preset's density read back);
 *    filament of the type's density weighed `since × type / implied`, and the scale finds
 *    `since × (1 − type / implied)` more than the ledger expected, where `since` is the
 *    grams booked between the previous anchor and the weighing (a drift covers only those).
 * 3. `empty_weight`: the spool's empty weight is a placeholder.
 */
import type { SpoolEvent, SpoolHistory } from '../../api/filament'

/**
 * #7682 — an added-full spool whose starting amount (its `created` event) came from
 * something else than a reading (a Bambuddy pull's grams) started at its label weight: the
 * label weight less that amount is the gap, unexplained. A later anchor (a sync, an AMS
 * reading) follows prints, so it has no gap. `null` under 0.05 g. `GramsBasis` says it.
 */
export function startGapG(anchor: SpoolEvent | null, addedFull: boolean | null, labelWeight: number | null): number | null {
  if (addedFull !== true || labelWeight == null || !anchor || anchor.grams_after == null) return null
  if (anchor.kind !== 'created' || anchor.source === 'scale' || anchor.source === 'person') return null
  const gap = labelWeight - anchor.grams_after
  return Math.abs(gap) < 0.05 ? null : gap
}

/** `ledger.reading`: the grams an absolute reading gives; `null` for an event that is not one. */
function readingOf(event: SpoolEvent, labelWeight: number | null): number | null {
  if (!event.source || !ABSOLUTE.has(event.source)) return null
  if (event.grams_after != null) return event.grams_after
  const pct = event.source === 'ams_remain' ? (event.data as { remain_pct?: unknown }).remain_pct : undefined
  return typeof pct === 'number' && labelWeight != null ? (pct * labelWeight) / 100 : null
}

/** `ledger.is_anchor`: a reading, and not an AMS one while the weight is locked or the AMS is printing. */
function isAnchor(event: SpoolEvent, weightLocked: boolean, labelWeight: number | null): boolean {
  return readingOf(event, labelWeight) != null && !(event.source === 'ams_remain' && (weightLocked || (event.data as { printing?: unknown }).printing === true))
}

const ABSOLUTE = new Set(['scale', 'person', 'bb', 'ams_remain'])

const order = (a: SpoolEvent, b: SpoolEvent) => Date.parse(a.at) - Date.parse(b.at) || a.seq - b.seq

export interface SpoolFacts {
  addedFull: boolean | null
  labelWeight: number | null
  /**
   * The spool's lock now. The weighing's `expected_g` used the lock as it was then, so an AMS
   * anchor can read differently here when the lock changed since; the cause is a sentence,
   * and that case is left as it is.
   */
  weightLocked: boolean
}

/**
 * The anchor the weighing was compared with: the newest anchor before it (`ledger.is_anchor`,
 * as `ledger.basis` read it for the weighing's `expected_g`), by time then seq. Before it in
 * time, strictly: `ledger.expected_before` keeps only the events before the weighing's `at`
 * and the deltas at that instant, so an anchor at the same instant, whatever its seq, was not
 * read. The weighing itself, an anchor too, is never its own.
 */
export function previousAnchor(events: readonly SpoolEvent[], weighing: SpoolEvent, facts: SpoolFacts): SpoolEvent | null {
  const at = Date.parse(weighing.at)
  return [...events].filter((event) => Date.parse(event.at) < at && isAnchor(event, facts.weightLocked, facts.labelWeight)).sort((a, b) => order(b, a))[0] ?? null
}

/**
 * The grams the ledger took off between `anchor` and the weighing: the anchor's grams less
 * what the ledger expected at the weighing (`expected_g`, the anchor less the deltas after
 * it), never negative; `null` when either is unknown.
 */
export function gramsSinceAnchor(anchor: SpoolEvent | null, expectedG: number | null, labelWeight: number | null): number | null {
  // The anchor's grams as `readingOf` reads them (#7932): a percentage-only AMS reading counts through the label weight.
  const found = anchor ? readingOf(anchor, labelWeight) : null
  if (found == null || expectedG == null) return null
  return Math.max(0, found - expectedG)
}

export type DriftCause = { kind: 'start_amount'; gap: number } | { kind: 'density'; implied: number; type: number } | { kind: 'empty_weight' }

export interface DriftInput {
  /** The weighing's `drift_g`: the filament found, less what the ledger expected. */
  driftG: number | null
  emptyBasis: string | null
  /** The label weight less the starting amount the weighing was compared with; `null` when there is no gap. */
  startGapG: number | null
  /** The grams the ledger took off between the previous anchor and the weighing (`gramsSinceAnchor`). */
  usedG: number | null
  impliedDensity: number | null
  typeDensity: number | null
  densityMismatch: boolean
}

/** `value` within `tolerance` of `target`, where the tolerance is a gram or a tenth of `target`. */
const near = (value: number, target: number) => Math.abs(value - target) <= Math.max(1, 0.1 * Math.abs(target))

export function driftCause({ driftG, emptyBasis, startGapG, usedG, impliedDensity, typeDensity, densityMismatch }: DriftInput): DriftCause | null {
  if (driftG == null || Math.abs(driftG) < 1) return null
  if (startGapG != null && near(driftG, startGapG)) return { kind: 'start_amount', gap: startGapG }
  if (densityMismatch && impliedDensity != null && typeDensity != null && impliedDensity > 0 && usedG != null && usedG > 0) {
    const explained = usedG * (1 - typeDensity / impliedDensity)
    if (Math.abs(explained) >= 1 && near(driftG, explained)) return { kind: 'density', implied: impliedDensity, type: typeDensity }
  }
  if (emptyBasis === 'placeholder') return { kind: 'empty_weight' }
  return null
}

/** The cause of `weighing`'s drift on the spool page: `driftCause` over the weighing's breakdown, the history's events and metrics, and the spool's facts. */
export function weighingCause(weighing: SpoolEvent, history: SpoolHistory, facts: SpoolFacts): DriftCause | null {
  const data = weighing.data as { drift_g?: unknown; expected_g?: unknown; empty_basis?: unknown }
  const anchor = previousAnchor(
    history.events.map((row) => row.event),
    weighing,
    facts,
  )
  return driftCause({
    driftG: typeof data.drift_g === 'number' ? data.drift_g : null,
    emptyBasis: typeof data.empty_basis === 'string' ? data.empty_basis : null,
    startGapG: startGapG(anchor, facts.addedFull, facts.labelWeight),
    usedG: gramsSinceAnchor(anchor, typeof data.expected_g === 'number' ? data.expected_g : null, facts.labelWeight),
    impliedDensity: history.metrics.implied_density_g_cm3 ?? null,
    typeDensity: history.metrics.density_g_cm3 ?? null,
    densityMismatch: history.metrics.density_mismatch ?? false,
  })
}
