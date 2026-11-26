import { describe, expect, it } from 'vitest'
import type { SpoolEvent, SpoolHistory, TimelineRow } from '../../api/filament'
import { driftCause, gramsSinceAnchor, previousAnchor, weighingCause, type DriftInput } from './driftCause'

const input = (over: Partial<DriftInput> = {}): DriftInput => ({
  driftG: -12,
  emptyBasis: 'catalog',
  startGapG: null,
  usedG: 300,
  impliedDensity: null,
  typeDensity: null,
  densityMismatch: false,
  ...over,
})

describe('driftCause (#7685, spec §5.6)', () => {
  it('names the starting amount when the scale found the label weight', () => {
    // Bambuddy said 979.3 g of a 1000 g label; the ledger anchored on 979.3; the scale finds 20.7 g more.
    expect(driftCause(input({ driftG: 20.7, startGapG: 20.7 }))).toEqual({ kind: 'start_amount', gap: 20.7 })
    // Within a tenth of the gap, or a gram.
    expect(driftCause(input({ driftG: 19, startGapG: 20.7 }))).toEqual({ kind: 'start_amount', gap: 20.7 })
    expect(driftCause(input({ driftG: 15, startGapG: 20.7 }))).toBeNull()
    // The gap the other way is no fit.
    expect(driftCause(input({ driftG: -20.7, startGapG: 20.7 }))).toBeNull()
    // A starting amount over the label: the scale finds less.
    expect(driftCause(input({ driftG: -20, startGapG: -20 }))).toEqual({ kind: 'start_amount', gap: -20 })
  })

  it('names the density when the drift is what the slicer over-counted', () => {
    // 300 g booked by a slicer at 1.31 of filament that is really the type's 1.24: the ledger
    // took 300 g off, the scale sees 300 × 1.24 / 1.31 = 284 g gone, 16 g more left than expected.
    const dense = input({ driftG: 16, usedG: 300, impliedDensity: 1.31, typeDensity: 1.24, densityMismatch: true })
    expect(driftCause(dense)).toEqual({ kind: 'density', implied: 1.31, type: 1.24 })
    // A drift that the densities do not explain is not blamed on them.
    expect(driftCause({ ...dense, driftG: 40 })).toBeNull()
    expect(driftCause({ ...dense, driftG: -16 })).toBeNull()
    // No mismatch, no cause; nor without the grams since the anchor.
    expect(driftCause({ ...dense, densityMismatch: false })).toBeNull()
    expect(driftCause({ ...dense, usedG: null })).toBeNull()
    // A second weighing after 100 g more covers those 100 g only: 100 × (1 − 1.24 / 1.31) ≈ 5.3 g.
    expect(driftCause({ ...dense, driftG: 5.3, usedG: 100 })).toEqual({ kind: 'density', implied: 1.31, type: 1.24 })
    expect(driftCause({ ...dense, driftG: 5.3, usedG: 400 })).toBeNull()
  })

  it('names a placeholder empty weight last', () => {
    expect(driftCause(input({ emptyBasis: 'placeholder' }))).toEqual({ kind: 'empty_weight' })
    // The starting amount comes first when it fits too.
    expect(driftCause(input({ emptyBasis: 'placeholder', driftG: 20.7, startGapG: 20.7 }))).toEqual({ kind: 'start_amount', gap: 20.7 })
  })

  it('names no cause under a gram of drift, or with no fit', () => {
    expect(driftCause(input({ driftG: 0.6, emptyBasis: 'placeholder', startGapG: 0.6 }))).toBeNull()
    expect(driftCause(input({ driftG: null, emptyBasis: 'placeholder' }))).toBeNull()
    expect(driftCause(input())).toBeNull()
  })

  it('names no cause without a history', () => {
    expect(driftCause(input({ driftG: -12, impliedDensity: null, typeDensity: null, densityMismatch: false, startGapG: null }))).toBeNull()
  })
})

let seq = 0
const event = (at: string, kind: SpoolEvent['kind'], values: Partial<SpoolEvent> = {}): SpoolEvent => ({
  id: `e${++seq}`,
  seq,
  spool_id: 's1',
  at,
  kind,
  source: null,
  printer_id: null,
  ams_id: null,
  tray_id: null,
  grams_before: null,
  grams_after: null,
  delta_g: null,
  ref_kind: null,
  ref_id: null,
  actor: null,
  data: {},
  ...values,
})
const row = (e: SpoolEvent): TimelineRow => ({ event: e, order_id: null, archive_id: null })

const created = event('2026-11-01T09:00:00Z', 'created', { source: 'bb', grams_after: 268 })
const printed = event('2026-11-14T21:30:00Z', 'print_ended', { source: 'print_actual', grams_before: 268, grams_after: 120, delta_g: -148, ref_kind: 'print_run', ref_id: 'r/j/1', data: { job: 'j', basis: 'sliced' } })
const ams = event('2026-11-15T08:00:00Z', 'ams_reading', { source: 'ams_remain', grams_after: 118 })
const weighed = event('2026-11-16T10:00:00Z', 'weighed', { source: 'scale', grams_after: 852, data: { reading_g: 1102, empty_g: 250, empty_source: 'insert', empty_basis: 'catalog', empty_name: 'Bambu reusable spool', expected_g: 120, drift_g: 732 } })

const facts = { addedFull: true, labelWeight: 1000, weightLocked: false }

const history = (events: SpoolEvent[], over: Partial<SpoolHistory['metrics']> = {}): SpoolHistory => ({
  events: [...events].reverse().map(row),
  series: [],
  metrics: {
    used_g: 148,
    consumed_g: 148,
    prints: 1,
    grams_per_print: 148,
    wasted_g: 0,
    cost_used: null,
    value_left: null,
    burn_g_per_day: null,
    empty_on: null,
    opened_at: null,
    days_since_opened: null,
    days_since_last_use: null,
    hours_outside_dry: 0,
    ams_error: null,
    used_m: null,
    implied_density_g_cm3: null,
    density_g_cm3: null,
    density_mismatch: false,
    ...over,
  },
  type_ams_error: null,
  currency: 'USD',
  window_days: 30,
})

describe('the anchor a weighing was compared with (#7685)', () => {
  it('is the newest anchor before the weighing, never the weighing itself', () => {
    expect(previousAnchor([created, printed, weighed], weighed, facts)).toBe(created)
    expect(previousAnchor([created, printed, ams, weighed], weighed, facts)).toBe(ams)
    // The order of the list does not matter; the events' times and seqs do.
    expect(previousAnchor([weighed, ams, printed, created], weighed, facts)).toBe(ams)
  })

  it('skips an AMS reading while the weight is locked or the AMS is printing (ledger.is_anchor)', () => {
    expect(previousAnchor([created, ams, weighed], weighed, { ...facts, weightLocked: true })).toBe(created)
    const printing = event('2026-11-15T09:00:00Z', 'ams_reading', { source: 'ams_remain', grams_after: 117, data: { printing: true } })
    expect(previousAnchor([created, ams, printing, weighed], weighed, facts)).toBe(ams)
    // An AMS percentage reads against the label weight.
    const pct = event('2026-11-15T09:30:00Z', 'ams_reading', { source: 'ams_remain', data: { remain_pct: 11 } })
    expect(previousAnchor([created, pct, weighed], weighed, facts)).toBe(pct)
    expect(previousAnchor([created, pct, weighed], weighed, { ...facts, labelWeight: null })).toBe(created)
  })

  it('is none when nothing anchored before it', () => {
    expect(previousAnchor([weighed, printed], weighed, facts)).toBeNull()
  })

  it('skips an anchor at the weighing\'s own instant, as ledger.expected_before does', () => {
    // Same `at`, a lower seq: `expected_before` keeps only deltas at that instant, so it was not read.
    const sameInstant = { ...event('2026-11-16T10:00:00Z', 'ams_reading', { source: 'ams_remain', grams_after: 119 }), seq: weighed.seq - 1 }
    expect(previousAnchor([created, sameInstant, weighed], weighed, facts)).toBe(created)
  })

  it('gives the grams the ledger took off since it', () => {
    expect(gramsSinceAnchor(created, 120, null)).toBe(148)
    expect(gramsSinceAnchor(created, 300, null)).toBe(0)
    expect(gramsSinceAnchor(null, 120, null)).toBeNull()
    expect(gramsSinceAnchor(created, null, null)).toBeNull()
  })

  it('reads a percentage-only AMS anchor through the label weight, as the anchor rule does (#7932)', () => {
    const pct = event('2026-11-15T09:30:00Z', 'ams_reading', { source: 'ams_remain', data: { remain_pct: 11 } })
    expect(gramsSinceAnchor(pct, 100, 1000)).toBe(10)
    expect(gramsSinceAnchor(pct, 100, null)).toBeNull()
  })
})

describe('the cause of a weighing on the spool page (#7685)', () => {
  it('names the starting amount for an added-full spool whose scale reading is the label weight less its prints', () => {
    expect(weighingCause(weighed, history([created, printed, weighed]), facts)).toEqual({ kind: 'start_amount', gap: 732 })
    // Not added full, or an anchor after the start: no gap to name.
    expect(weighingCause(weighed, history([created, printed, weighed]), { ...facts, addedFull: false })).toBeNull()
    expect(weighingCause(weighed, history([created, printed, ams, weighed]), facts)).toBeNull()
  })

  it('names the density cause against a percentage-only AMS anchor (#7932)', () => {
    // The anchor is 11 % of the 1000 g label, 110 g; the ledger expected 100 g at the weighing, so 10 g were used
    // since it, and a drift of about 10 × (1 − 1.24 / 1.31) ≈ 0.5 g is under a gram: no cause. At 60 g used
    // (the ledger expected 50 g) the drift 60 × (1 − 1.24 / 1.31) ≈ 3.2 g names the density.
    const pct = event('2026-11-15T09:30:00Z', 'ams_reading', { source: 'ams_remain', data: { remain_pct: 11 } })
    const weighing = event('2026-11-16T10:00:00Z', 'weighed', { source: 'scale', grams_after: 53, data: { reading_g: 303, empty_g: 250, empty_source: 'insert', empty_basis: 'catalog', expected_g: 50, drift_g: 3.2 } })
    const over = { implied_density_g_cm3: 1.31, density_g_cm3: 1.24, density_mismatch: true }
    expect(weighingCause(weighing, history([created, printed, pct, weighing], over), { ...facts, addedFull: false })).toEqual({ kind: 'density', implied: 1.31, type: 1.24 })
    const little = event('2026-11-16T10:00:00Z', 'weighed', { source: 'scale', grams_after: 100.5, data: { reading_g: 350.5, empty_g: 250, empty_source: 'insert', empty_basis: 'catalog', expected_g: 100, drift_g: 0.5 } })
    expect(weighingCause(little, history([created, printed, pct, little], over), { ...facts, addedFull: false })).toBeNull()
  })

  it('scales the density cause by the grams since the previous anchor', () => {
    // Since the AMS anchor at 118 g the ledger expected 100 g less: 100 × (1 − 1.24 / 1.31) ≈ 5.3 g.
    const again = event('2026-11-20T10:00:00Z', 'weighed', { source: 'scale', grams_after: 23.3, data: { reading_g: 273.3, empty_g: 250, empty_source: 'insert', empty_basis: 'catalog', empty_name: 'Bambu reusable spool', expected_g: 18, drift_g: 5.3 } })
    const dense = { used_g: 400, implied_density_g_cm3: 1.31, density_g_cm3: 1.24, density_mismatch: true }
    expect(weighingCause(again, history([created, printed, ams, again], dense), facts)).toEqual({ kind: 'density', implied: 1.31, type: 1.24 })
  })
})
