import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SpoolEvent } from '../../api/filament'
import { WeighingBreakdown } from './WeighingBreakdown'

const weighed = (values: Partial<SpoolEvent>): SpoolEvent => ({
  id: 'e1',
  seq: 1,
  spool_id: 's1',
  at: '2026-11-25T10:00:00Z',
  kind: 'weighed',
  source: 'scale',
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

const insert = { reading_g: 1180, empty_g: 200, empty_source: 'insert', empty_basis: 'placeholder', empty_name: 'Plastic' }

describe('WeighingBreakdown (#7682)', () => {
  it('says what the reading was made of and how it compares', () => {
    render(<WeighingBreakdown event={weighed({ grams_after: 980, data: { ...insert, expected_g: 992, drift_g: -12 } })} />)
    expect(screen.getByText(/Weighed 1180 g on .*: filament 980 g \+ spool 200 g \(Plastic, placeholder\)\./)).toBeInTheDocument()
    expect(screen.getByText(/The ledger expected 992 g\. The scale found 12 g less than the prints accounted for\./)).toBeInTheDocument()
  })

  it('shows only the breakdown when nothing was expected', () => {
    render(<WeighingBreakdown event={weighed({ grams_after: 980, data: { ...insert, empty_basis: 'catalog', expected_g: null, drift_g: null } })} />)
    expect(screen.getByText(/filament 980 g \+ spool 200 g \(Plastic, from the catalog\)\./)).toBeInTheDocument()
    expect(screen.queryByText(/ledger expected/)).toBeNull()
  })

  it('says the scale matches under a gram', () => {
    render(<WeighingBreakdown event={weighed({ grams_after: 980.4, data: { ...insert, empty_basis: 'derived', expected_g: 980, drift_g: 0.4 } })} />)
    expect(screen.getByText(/\(Plastic, from new spools\)\. The ledger expected 980 g\. The scale matches the prints\./)).toBeInTheDocument()
  })

  it('says more when the drift is positive', () => {
    render(<WeighingBreakdown event={weighed({ grams_after: 1000, data: { ...insert, empty_basis: 'weighed_empty', expected_g: 992, drift_g: 8 } })} />)
    expect(screen.getByText(/\(Plastic, weighed empty\)\. The ledger expected 992 g\. The scale found 8 g more than the prints accounted for\./)).toBeInTheDocument()
  })

  it('says the scale was tared on the spool, and compares nothing when the gap was explained (#7683)', () => {
    render(<WeighingBreakdown event={weighed({ grams_after: 900, data: { reading_g: 900, empty_g: 0, empty_source: 'tared', empty_basis: null, empty_name: null, expected_g: 1000, drift_g: null, resolution: 'tared', shortfall_g: 100 } })} />)
    expect(screen.getByText(/Weighed 900 g on .*: filament 900 g \(the scale was tared on the spool\)\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/spool 0 g|ledger expected|matches the prints/)).toBeNull()
  })

  it('names a person-taken empty weight', () => {
    render(<WeighingBreakdown event={weighed({ grams_after: 962, data: { reading_g: 1180, empty_g: 218, empty_source: 'person', empty_basis: null, empty_name: null, expected_g: null, drift_g: null } })} />)
    expect(screen.getByText(/filament 962 g \+ spool 218 g \(taken off by hand\)\./)).toBeInTheDocument()
  })
})
