import { describe, expect, it } from 'vitest'
import { driftCause, type DriftInput } from './driftCause'

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
  it('names the label weight when the drift is the start gap', () => {
    expect(driftCause(input({ driftG: -20.7, startGapG: 20.7 }))).toEqual({ kind: 'label_weight', gap: 20.7 })
    // Within a tenth of the gap, or a gram.
    expect(driftCause(input({ driftG: -19, startGapG: 20.7 }))).toEqual({ kind: 'label_weight', gap: 20.7 })
    expect(driftCause(input({ driftG: -15, startGapG: 20.7 }))).toBeNull()
    // The gap the other way is no fit.
    expect(driftCause(input({ driftG: 20.7, startGapG: 20.7 }))).toBeNull()
  })

  it('names the density when the drift is what the slicer over-counted', () => {
    // 300 g booked at the type's 1.24 of filament that is really 1.31: the ledger took 300 g off,
    // the scale sees 300 × 1.24 / 1.31 = 284 g gone, 16 g more left than expected.
    const dense = input({ driftG: 16, usedG: 300, impliedDensity: 1.31, typeDensity: 1.24, densityMismatch: true })
    expect(driftCause(dense)).toEqual({ kind: 'density', implied: 1.31, type: 1.24 })
    // A drift that the densities do not explain is not blamed on them.
    expect(driftCause({ ...dense, driftG: 40 })).toBeNull()
    expect(driftCause({ ...dense, driftG: -16 })).toBeNull()
    // No mismatch, no cause.
    expect(driftCause({ ...dense, densityMismatch: false })).toBeNull()
  })

  it('names a placeholder empty weight last', () => {
    expect(driftCause(input({ emptyBasis: 'placeholder' }))).toEqual({ kind: 'empty_weight' })
    // The label weight comes first when it fits too.
    expect(driftCause(input({ emptyBasis: 'placeholder', driftG: -20.7, startGapG: 20.7 }))).toEqual({ kind: 'label_weight', gap: 20.7 })
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
