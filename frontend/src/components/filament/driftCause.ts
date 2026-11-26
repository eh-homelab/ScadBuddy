/**
 * #7685 (spec §5.6) — the likely cause of a weighing's drift, from what the page already
 * knows: the start gap of an added-full spool (`startGapG`), the density the
 * prints imply against the type's (`SpoolMetrics`), and where the empty weight came from.
 * A sentence, never a booking. The first rule that fits wins, in this order:
 *
 * 1. `label_weight`: the scale found exactly the gap the label over-promised.
 * 2. `density`: the ledger took the slicer's grams, sliced at the type's density; filament
 *    of the prints' density would have weighed `usedG × type / implied`.
 * 3. `empty_weight`: the spool's empty weight is a placeholder.
 */
import type { SpoolEvent } from '../../api/filament'

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

export type DriftCause = { kind: 'label_weight'; gap: number } | { kind: 'density'; implied: number; type: number } | { kind: 'empty_weight' }

export interface DriftInput {
  /** The weighing's `drift_g`: the filament found, less what the ledger expected. */
  driftG: number | null
  emptyBasis: string | null
  /** The label weight less the starting amount; `null` when there is no gap to show. */
  startGapG: number | null
  /** The lifetime grams the prints booked (`SpoolMetrics.used_g`). */
  usedG: number
  impliedDensity: number | null
  typeDensity: number | null
  densityMismatch: boolean
}

/** `value` within `tolerance` of `target`, where the tolerance is a gram or a tenth of `target`. */
const near = (value: number, target: number) => Math.abs(value - target) <= Math.max(1, 0.1 * Math.abs(target))

export function driftCause({ driftG, emptyBasis, startGapG, usedG, impliedDensity, typeDensity, densityMismatch }: DriftInput): DriftCause | null {
  if (driftG == null || Math.abs(driftG) < 1) return null
  if (startGapG != null && near(driftG, -startGapG)) return { kind: 'label_weight', gap: startGapG }
  if (densityMismatch && impliedDensity != null && typeDensity != null && impliedDensity > 0 && usedG > 0) {
    const explained = usedG * (1 - typeDensity / impliedDensity)
    if (Math.abs(explained) >= 1 && near(driftG, explained)) return { kind: 'density', implied: impliedDensity, type: typeDensity }
  }
  if (emptyBasis === 'placeholder') return { kind: 'empty_weight' }
  return null
}
