import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LineChart, niceMax } from './LineChart'

const points = [
  { at: Date.parse('2026-11-01T00:00:00Z'), value: 1000 },
  { at: Date.parse('2026-11-05T00:00:00Z'), value: 700 },
  { at: Date.parse('2026-11-09T00:00:00Z'), value: 400 },
]

/** The path's x and y coordinates, in order. */
function coordinates(d: string): { xs: number[]; ys: number[] } {
  const numbers = [...d.matchAll(/-?\d+(\.\d+)?/g)].map((match) => Number(match[0]))
  const xs: number[] = []
  const ys: number[] = []
  for (let i = 0; i < numbers.length; i += 2) {
    xs.push(numbers[i]!)
    ys.push(numbers[i + 1]!)
  }
  return { xs, ys }
}

describe('the line chart', () => {
  it('draws a path that spans the points\' range: first to last time, highest to lowest value', () => {
    const { container } = render(<LineChart points={points} label="Grams" formatValue={String} width={320} height={120} />)
    expect(screen.getByRole('img', { name: 'Grams' })).toBeInTheDocument()
    const { xs, ys } = coordinates(container.querySelector('path[data-line]')!.getAttribute('d')!)
    expect(xs).toHaveLength(3)
    expect(xs[0]).toBe(36)
    expect(xs.at(-1)).toBe(320 - 8)
    // A zero-based scale to 1000: the top value at the plot's top, 400 at 40 % of its height.
    expect(Math.min(...ys)).toBe(8)
    expect(Math.max(...ys)).toBeCloseTo(8 + 94 - 0.4 * 94, 1)
  })

  it('draws a step line as horizontal then vertical moves', () => {
    const { container } = render(<LineChart points={points} label="Grams" formatValue={String} step />)
    expect(container.querySelector('path[data-line]')!.getAttribute('d')).toMatch(/^M[\d.]+ [\d.]+ H[\d.]+ V[\d.]+ H[\d.]+ V[\d.]+$/)
  })

  it('names its marks and ticks by the attribute it is given', () => {
    const { container } = render(<LineChart points={points} label="Grams" formatValue={String} marks={{ name: 'anchor', points: points.slice(0, 1) }} ticks={{ name: 'plate', at: [points[1]!.at] }} />)
    expect(container.querySelectorAll('circle[data-anchor]')).toHaveLength(1)
    expect(container.querySelectorAll('line[data-plate]')).toHaveLength(1)
  })

  it('rounds the top of the scale up to a round number', () => {
    expect([niceMax(995), niceMax(268), niceMax(1000), niceMax(0)]).toEqual([1000, 300, 1000, 1])
  })
})
