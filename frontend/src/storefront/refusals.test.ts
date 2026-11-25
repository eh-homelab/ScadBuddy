import { describe, expect, it } from 'vitest'
import { inTime } from './refusals'

describe('inTime (#7523)', () => {
  it.each([
    [null, 'in a minute'],
    [1, 'in 1 second'],
    [42, 'in 42 seconds'],
    [90, 'in 90 seconds'],
    [91, 'in 2 minutes'],
    [700, 'in 12 minutes'],
    [5_400, 'in 90 minutes'],
    [5_401, 'in 2 hours'],
    [79_000, 'in 22 hours'],
  ])('says %s as "%s"', (seconds, text) => {
    expect(inTime(seconds)).toBe(text)
  })
})
