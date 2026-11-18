import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { GramsPoint, SpoolEvent } from '../../api/filament'
import { plateTimes } from './eventText'
import { GramsChart } from './GramsChart'

const points: GramsPoint[] = [
  { at: '2026-11-01T09:00:00Z', grams: 1000, anchor: true, kind: 'weighed' },
  { at: '2026-11-03T20:00:00Z', grams: 945, anchor: false, kind: 'print_ended' },
  { at: '2026-11-06T16:00:00Z', grams: 910, anchor: false, kind: 'print_ended' },
]

describe('the grams chart', () => {
  it('marks the one anchor reading, ticks each plate, and names its range for a screen reader', () => {
    const { container } = render(<GramsChart points={points} plates={['2026-11-03T20:00:00Z', '2026-11-06T16:00:00Z']} label="Grams left" />)
    expect(container.querySelectorAll('circle[data-anchor]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-plate]')).toHaveLength(2)
    expect(screen.getByRole('img', { name: 'Grams left: 1000 g to 910 g, 1 anchor reading' })).toHaveAttribute('viewBox', '0 0 320 120')
  })

  it('says so when there are no points, and draws nothing', () => {
    const { container } = render(<GramsChart points={[]} plates={[]} label="Grams left" />)
    expect(screen.getByText('No weight history yet.')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
  })

  it('draws the readings in the accent and the plates in ink, so one colour means one thing', () => {
    const { container } = render(<GramsChart points={points} plates={['2026-11-03T20:00:00Z']} label="Grams left" />)
    expect(container.querySelector('circle[data-anchor]')!.closest('.text-primary')).not.toBeNull()
    expect(container.querySelector('[data-plate]')!.closest('g')).toHaveClass('text-foreground')
  })
})

let seq = 0
const event = (values: Partial<SpoolEvent>): SpoolEvent => ({
  id: `e${++seq}`,
  seq,
  spool_id: 's1',
  at: '2026-11-10T10:00:00Z',
  kind: 'print_ended',
  source: 'print_actual',
  printer_id: null,
  ams_id: null,
  tray_id: null,
  grams_before: null,
  grams_after: null,
  delta_g: -20,
  ref_kind: 'print_run',
  ref_id: null,
  actor: null,
  data: {},
  ...values,
})

describe('the plate ticks (plan Decision 65)', () => {
  it('gives one tick per plate, as metrics.plates counts them: a job SB and BB both booked once, a covered or not-applied end none', () => {
    const at = plateTimes([
      event({ at: '2026-11-01T10:00:00Z', ref_kind: 'print_run', ref_id: 'run1/job-a/0', data: { job: 'job-a' } }),
      event({ at: '2026-11-01T10:01:00Z', ref_kind: 'bb_usage', ref_id: 'usage-a', data: { job: 'job-a' } }),
      event({ at: '2026-11-02T10:00:00Z', ref_kind: 'print_run', ref_id: 'run2/job-b/0', data: { job: 'job-b', basis: 'covered' } }),
      event({ at: '2026-11-03T10:00:00Z', ref_kind: 'print_run', ref_id: 'run3/job-c/0', delta_g: 0, data: { job: 'job-c', basis: 'not_applied' } }),
      event({ at: '2026-11-04T10:00:00Z', kind: 'print_started', source: 'print_estimate', ref_kind: 'print_run', ref_id: 'run4/job-d/0', data: { job: 'job-d' } }),
      event({ at: '2026-11-05T10:00:00Z', ref_kind: 'print_run', ref_id: 'run5/job-e/0', data: { job: 'job-e' } }),
    ])
    expect(at).toEqual(['2026-11-01T10:01:00Z', '2026-11-05T10:00:00Z'])
  })
})
