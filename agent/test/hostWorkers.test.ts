import { describe, expect, it } from 'vitest'
import { PINNED_SHUTDOWN_FORCE_MS, PINNED_SHUTDOWN_GRACE_MS, stopAfter } from '../src/temporal/session/hostWorkers.js'

// #5862 review: the pinned host worker runs this replica's own BridgeCalls, so a draining turn's
// browser_* call needs it until the turn ends. It must stop only once the drain settles.
describe('stopAfter', () => {
  it('stops the worker only once the drain settles', async () => {
    const order: string[] = []
    let endDrain!: () => void
    const drained = new Promise<void>((resolve) => (endDrain = resolve))
    const stopped = stopAfter({ stop: async () => void order.push('stop') }, drained)
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(order).toEqual([])
    order.push('drained')
    endDrain()
    await stopped
    expect(order).toEqual(['drained', 'stop'])
  })

  it('still stops the worker when the drain fails', async () => {
    let stops = 0
    await stopAfter({ stop: async () => void stops++ }, Promise.reject(new Error('a turn failed to stop')))
    expect(stops).toBe(1)
  })

  it("resolves only once the worker's stop does, for the shutdown to await before the pool closes", async () => {
    let endStop!: () => void
    let done = false
    const stopped = stopAfter({ stop: () => new Promise<void>((resolve) => (endStop = resolve)) }, Promise.resolve()).then(() => (done = true))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(done).toBe(false)
    endStop()
    await stopped
    expect(done).toBe(true)
  })

  it('never rejects when the worker fails to stop, so an early shutdown failure leaves no unhandled rejection', async () => {
    await expect(stopAfter({ stop: () => Promise.reject(new Error('stuck')) }, Promise.resolve())).resolves.toBeUndefined()
  })

  it("ends inside shutdown()'s 10 s deadline with a margin (#5881 review)", () => {
    expect(PINNED_SHUTDOWN_GRACE_MS).toBeLessThan(PINNED_SHUTDOWN_FORCE_MS)
    expect(PINNED_SHUTDOWN_FORCE_MS).toBeLessThanOrEqual(8_000)
  })
})
