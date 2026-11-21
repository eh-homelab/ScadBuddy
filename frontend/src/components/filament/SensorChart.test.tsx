import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SensorChart } from './SensorChart'

const readings = [
  { at: '2026-11-18T08:00:00Z', value: 50 },
  { at: '2026-11-18T09:00:00Z', value: 58 },
  { at: '2026-11-18T10:00:00Z', value: 62 },
]

describe('the sensor chart', () => {
  it('names its range and unit for a screen reader, and draws one line through every reading', () => {
    const { container } = render(<SensorChart readings={readings} unit="%" label="Shelf A humidity" />)
    expect(screen.getByRole('img', { name: 'Shelf A humidity: 50 to 62 %, 3 readings' })).toBeInTheDocument()
    expect(container.querySelector('polyline')?.getAttribute('points')?.trim().split(' ')).toHaveLength(3)
  })

  it('draws a limit line only for a limit that is set', () => {
    const { container, rerender } = render(<SensorChart readings={readings} unit="%" label="x" alertAbove={55} />)
    expect(container.querySelectorAll('[data-limit]')).toHaveLength(1)
    expect(container.querySelector('[data-limit="above"]')).not.toBeNull()
    rerender(<SensorChart readings={readings} unit="%" label="x" />)
    expect(container.querySelectorAll('[data-limit]')).toHaveLength(0)
  })

  it('says so when there are no readings, and draws nothing', () => {
    const { container } = render(<SensorChart readings={[]} unit="%" label="x" />)
    expect(screen.getByText('No readings in this time yet.')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
  })

  it('draws a single reading as a dot, not a line', () => {
    const { container } = render(<SensorChart readings={readings.slice(0, 1)} unit="%" label="x" />)
    expect(container.querySelector('circle')).not.toBeNull()
  })
})
