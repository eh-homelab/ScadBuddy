import { describe, expect, it } from 'vitest'
import { hopUntilText } from './flows'

// #2482 — when a hop ends: the time alone today, with the date on any other day.
describe('hopUntilText', () => {
  it('says the time alone when the hop ends today', () => {
    const now = new Date(2026, 9, 13, 9, 0)
    const at = new Date(2026, 9, 13, 23, 30)
    expect(hopUntilText(at.toISOString(), now)).toBe(at.toLocaleTimeString())
  })

  it('names the date when the hop ends on another day', () => {
    const now = new Date(2026, 9, 13, 23, 55)
    const at = new Date(2026, 9, 14, 0, 5)
    expect(hopUntilText(at.toISOString(), now)).toBe(at.toLocaleString())
    expect(hopUntilText(at.toISOString(), now)).not.toBe(at.toLocaleTimeString())
  })
})
