import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renewLoop, SLOW_RENEW_MS } from '../src/sessions/manager.js'

// #2375: a turn's lease renewals, one at a time, and the slow or failed ones logged.

describe('renewLoop', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('starts no renewal while one is still waiting, and logs it once it is slow', async () => {
    let calls = 0
    let release!: () => void
    const logged: string[] = []
    const loop = renewLoop(
      () => {
        calls++
        return new Promise<void>((resolve) => (release = resolve))
      },
      1_000,
      (line) => logged.push(line),
    )
    await vi.advanceTimersByTimeAsync(1_000)
    expect(calls).toBe(1)
    // A stalled pool: ten more ticks join nothing to it.
    await vi.advanceTimersByTimeAsync(10_000)
    expect(calls).toBe(1)
    // Still waiting at SLOW_RENEW_MS: said then, once, not only when it ends (#2981 review).
    expect(logged).toEqual([expect.stringMatching(/^lease renewal still waiting after \d+ ms$/)])
    expect(Number(/(\d+) ms/.exec(logged[0]!)![1])).toBeGreaterThanOrEqual(SLOW_RENEW_MS)
    release()
    await vi.advanceTimersByTimeAsync(0)
    expect(logged).toEqual([expect.anything(), expect.stringMatching(/^lease renewal took \d+ ms$/)])
    expect(Number(/(\d+) ms/.exec(logged[1]!)![1])).toBeGreaterThanOrEqual(SLOW_RENEW_MS)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(calls).toBe(2)
    release()
    await loop.stop()
  })

  it('logs a failed renewal and keeps renewing', async () => {
    let calls = 0
    const logged: string[] = []
    const loop = renewLoop(
      async () => {
        calls++
        if (calls === 1) throw new Error('connection terminated')
      },
      1_000,
      (line) => logged.push(line),
    )
    await vi.advanceTimersByTimeAsync(2_000)
    expect(calls).toBe(2)
    expect(logged).toEqual([expect.stringMatching(/^lease renewal failed after \d+ ms: connection terminated$/)])
    await loop.stop()
  })

  it('logs a renewal that both fails and is slow once, its failure with its time (#3321)', async () => {
    let fail!: (err: Error) => void
    const logged: string[] = []
    const loop = renewLoop(
      () => new Promise<void>((_, reject) => (fail = reject)),
      1_000,
      (line) => logged.push(line),
    )
    await vi.advanceTimersByTimeAsync(1_000 + SLOW_RENEW_MS)
    fail(new Error('connection terminated'))
    await vi.advanceTimersByTimeAsync(0)
    // Its end is one line, the failure with its time; the wait before it was said once.
    expect(logged).toEqual([
      expect.stringMatching(/^lease renewal still waiting after \d+ ms$/),
      expect.stringMatching(/^lease renewal failed after (\d+) ms: connection terminated$/),
    ])
    expect(Number(/after (\d+) ms/.exec(logged[1]!)![1])).toBeGreaterThanOrEqual(SLOW_RENEW_MS)
    await loop.stop()
  })

  it('stop waits for the renewal in flight and starts no more', async () => {
    let calls = 0
    let release!: () => void
    const loop = renewLoop(
      () => {
        calls++
        return new Promise<void>((resolve) => (release = resolve))
      },
      1_000,
      () => {},
    )
    await vi.advanceTimersByTimeAsync(1_000)
    let stopped = false
    const stopping = loop.stop().then(() => (stopped = true))
    await vi.advanceTimersByTimeAsync(5_000)
    expect(stopped).toBe(false)
    release()
    await stopping
    expect(calls).toBe(1)
  })
})
