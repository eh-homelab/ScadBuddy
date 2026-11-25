import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SpoolEvent } from '../../api/filament'
import { GramsBasis } from './GramsBasis'

const anchor = (values: Partial<SpoolEvent>): SpoolEvent => ({
  id: 'e1',
  seq: 1,
  spool_id: 's1',
  at: '2026-11-01T09:00:00Z',
  kind: 'created',
  source: 'bb',
  printer_id: null,
  ams_id: null,
  tray_id: null,
  grams_before: null,
  grams_after: 979.3,
  delta_g: null,
  ref_kind: 'bb_spool',
  ref_id: 'bb-1',
  actor: null,
  data: {},
  ...values,
})

describe('GramsBasis (#7682)', () => {
  it('shows the unexplained start gap of an added-full spool', () => {
    render(<GramsBasis basis={{ anchor: anchor({}), deltas: [] }} gramsLeft={979.3} addedFull={true} labelWeight={1000} />)
    expect(screen.getByText('Started at the label weight 1000 g. Bambuddy reported 979.3 g; 20.7 g unexplained.')).toBeInTheDocument()
    expect(screen.getByText('Starting amount 979.3 g, Bambuddy')).toBeInTheDocument()
  })

  it('shows no gap for a scale anchor', () => {
    render(<GramsBasis basis={{ anchor: anchor({ kind: 'weighed', source: 'scale', ref_kind: null, ref_id: null }), deltas: [] }} gramsLeft={979.3} addedFull={true} labelWeight={1000} />)
    expect(screen.queryByText(/unexplained/)).toBeNull()
  })

  it('shows no gap for a Bambuddy sync or an AMS reading that re-anchored a used spool', () => {
    render(<GramsBasis basis={{ anchor: anchor({ kind: 'synced_from_bb', grams_after: 300 }), deltas: [] }} gramsLeft={300} addedFull={true} labelWeight={1000} />)
    expect(screen.queryByText(/unexplained/)).toBeNull()
    render(<GramsBasis basis={{ anchor: anchor({ kind: 'ams_reading', source: 'ams_remain', grams_after: 300 }), deltas: [] }} gramsLeft={300} addedFull={true} labelWeight={1000} />)
    expect(screen.queryByText(/unexplained/)).toBeNull()
  })

  it('shows no gap when the spool was not added full', () => {
    render(<GramsBasis basis={{ anchor: anchor({}), deltas: [] }} gramsLeft={979.3} addedFull={null} labelWeight={1000} />)
    expect(screen.queryByText(/unexplained/)).toBeNull()
    render(<GramsBasis basis={{ anchor: anchor({ grams_after: 1000 }), deltas: [] }} gramsLeft={1000} addedFull={true} labelWeight={1000} />)
    expect(screen.queryByText(/unexplained/)).toBeNull()
  })
})
