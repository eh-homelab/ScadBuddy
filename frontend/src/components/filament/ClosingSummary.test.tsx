import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ClosingSummary, closingText } from './ClosingSummary'

const AT = '2026-11-25T18:00:00Z'
const date = new Date(AT).toLocaleDateString(undefined, { dateStyle: 'medium' })

describe('ClosingSummary (#7684)', () => {
  it('says the reason, the start, the two consumptions and the drift', () => {
    render(<ClosingSummary closing={{ reason: 'finished', started_g: 1000, started_from: 'label', ledger_consumed_g: 508, final_filament_g: 480, scale_consumed_g: 520, drift_g: -12 }} at={AT} />)
    expect(screen.getByText(`Finished on ${date}: started with 1000 g (from the label), the ledger used 508 g, the scale says 520 g were used; 12 g less than the ledger expected.`)).toBeInTheDocument()
  })

  it('says not weighed at the end without a final reading, and not known for what it has no number for', () => {
    expect(closingText({ reason: 'damaged', started_g: 992, started_from: 'reading', ledger_consumed_g: 40, final_filament_g: 952, scale_consumed_g: null, drift_g: null }, AT)).toBe(`Damaged on ${date}: started with 992 g (first weighing), the ledger used 40 g; not weighed at the end.`)
    expect(closingText({ reason: null, started_g: null, started_from: null, ledger_consumed_g: null, final_filament_g: null, scale_consumed_g: null, drift_g: null }, AT)).toBe(`Archived on ${date}: started with not known, the ledger used not known; not weighed at the end.`)
  })

  it('says more when the scale found less filament than the ledger expected the other way round', () => {
    expect(closingText({ reason: 'other', started_g: 1000, started_from: 'label', ledger_consumed_g: 500, final_filament_g: 510, scale_consumed_g: 490, drift_g: 10 }, AT)).toContain('10 g more than the ledger expected.')
  })
})
