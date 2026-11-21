import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LineChart } from './LineChart'
import { niceMax, tickDecimal } from './scale'

const points = [
  { at: Date.parse('2026-11-01T00:00:00Z'), value: 1000 },
  { at: Date.parse('2026-11-05T00:00:00Z'), value: 700 },
  { at: Date.parse('2026-11-09T00:00:00Z'), value: 350 },
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
    // A zero-based scale to 1000 (the lowest value is below 40 % of the top): the top value at the plot's top, 350 at 35 % of its height.
    expect(Math.min(...ys)).toBe(8)
    expect(Math.max(...ys)).toBeCloseTo(8 + 94 - 0.35 * 94, 1)
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

  it('starts the scale below the lowest value, not at zero, when every value is in the top 60 %', () => {
    const high = [
      { at: Date.parse('2026-11-01T00:00:00Z'), value: 1000 },
      { at: Date.parse('2026-11-05T00:00:00Z'), value: 905 },
      { at: Date.parse('2026-11-09T00:00:00Z'), value: 820 },
    ]
    const { container } = render(<LineChart points={high} label="Grams" formatValue={String} width={320} height={120} />)
    expect([...container.querySelectorAll('text')].slice(0, 3).map((text) => text.textContent)).toEqual(['800', '900', '1000'])
    const { ys } = coordinates(container.querySelector('path[data-line]')!.getAttribute('d')!)
    // 1000 at the plot's top, 820 a tenth of the way up from 800 at its bottom.
    expect(Math.min(...ys)).toBe(8)
    expect(Math.max(...ys)).toBeCloseTo(8 + 94 - 0.1 * 94, 1)
  })

  it('keeps the hover value out of the accessibility tree: the label names the chart', async () => {
    const { container } = render(<LineChart points={points} label="Grams" formatValue={(value) => `${value} g`} />)
    const svg = container.querySelector('svg')!
    svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 320, height: 120, right: 320, bottom: 120, x: 0, y: 0, toJSON: () => ({}) })
    fireEvent.pointerMove(svg, { clientX: 312, clientY: 60 })
    const tip = await screen.findByText('350 g', { ignore: 'svg *' })
    expect(tip.closest('[aria-hidden="true"]')).not.toBeNull()
  })

  it('rounds the top of the scale up to a round number', () => {
    expect([niceMax(995), niceMax(268), niceMax(1000), niceMax(0)]).toEqual([1000, 300, 1000, 1])
  })

  it('writes a tick as its exact decimal string, dropping the float noise (R-T8-2)', () => {
    expect([tickDecimal(17.5), tickDecimal(0.1 + 0.2), tickDecimal(1200), tickDecimal(0)]).toEqual(['17.5', '0.3', '1200', '0'])
  })
})
