import { afterEach, describe, expect, it, vi } from 'vitest'
import { waitFor } from './highlight'

/** Date.now() moves by `stepMs` more on every read, as a stepping wall clock does (#1485). */
function steppingWallClock(stepMs: number): void {
  const real = Date.now.bind(Date)
  let offset = 0
  vi.spyOn(Date, 'now').mockImplementation(() => {
    offset += stepMs
    return real() + offset
  })
}

describe('waitFor on a stepping wall clock (#1485)', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keeps waiting when the wall clock jumps forward', async () => {
    let reads = 0
    steppingWallClock(3_600_000)
    const found = await waitFor(() => (++reads >= 3 ? 'here' : undefined), { timeout: 5_000, what: 'it', interval: 1 })
    expect(found).toBe('here')
  })

  it('gives up on time when the wall clock jumps back', async () => {
    steppingWallClock(-3_600_000)
    await expect(waitFor(() => undefined, { timeout: 30, what: 'it', interval: 1 })).rejects.toThrow('Gave up after 30 ms')
  }, 2_000)
})
