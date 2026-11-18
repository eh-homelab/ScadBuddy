import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { GramsPoint } from '../../api/filament'
import { GramsChart } from './GramsChart'

const points: GramsPoint[] = [
  { at: '2026-11-01T09:00:00Z', grams: 1000, anchor: true, kind: 'weighed' },
  { at: '2026-11-03T20:00:00Z', grams: 945, anchor: false, kind: 'print_ended' },
  { at: '2026-11-06T16:00:00Z', grams: 910, anchor: false, kind: 'print_ended' },
]

describe('the grams chart', () => {
  it('marks the one anchor reading, ticks each plate, and names its range for a screen reader', () => {
    const { container } = render(<GramsChart points={points} label="Grams left" />)
    expect(container.querySelectorAll('circle[data-anchor]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-plate]')).toHaveLength(2)
    expect(screen.getByRole('img', { name: 'Grams left: 1000 g to 910 g, 1 anchor reading' })).toHaveAttribute('viewBox', '0 0 320 120')
  })

  it('says so when there are no points, and draws nothing', () => {
    const { container } = render(<GramsChart points={[]} label="Grams left" />)
    expect(screen.getByText('No weight history yet.')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
  })
})
