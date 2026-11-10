import { describe, expect, it } from 'vitest'
import { stopAfter } from '../src/temporal/session/hostWorkers.js'

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
})
