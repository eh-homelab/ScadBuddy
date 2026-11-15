import { afterEach, describe, expect, it, vi } from 'vitest'
import { PINNED_SHUTDOWN_FORCE_MARGIN_MS, PINNED_SHUTDOWN_FORCE_MS, PINNED_SHUTDOWN_GRACE_MS, startHostWorkers, stopAfter } from '../src/temporal/session/hostWorkers.js'
import { AgentWorker, type AgentWorkerOptions } from '../src/temporal/worker.js'

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

describe('startHostWorkers', () => {
  afterEach(() => vi.restoreAllMocks())

  function pinnedOptions(extra: { shutdownGraceMs?: number } = {}): AgentWorkerOptions {
    const started: AgentWorkerOptions[] = []
    vi.spyOn(AgentWorker, 'start').mockImplementation((options) => {
      started.push(options)
      return { stop: async () => {} } as unknown as AgentWorker
    })
    startHostWorkers({ address: 'localhost:7233', namespace: 'test', identity: 'pod-a', pinned: {}, ...extra })
    const pinned = started.find((o) => o.taskQueue === 'agent-tools.pod-a')
    expect(pinned).toBeDefined()
    return pinned!
  }

  it('gives the pinned worker the bounded grace and force times (#5886 review)', () => {
    const pinned = pinnedOptions()
    expect(pinned.shutdownGraceMs).toBe(PINNED_SHUTDOWN_GRACE_MS)
    expect(pinned.shutdownForceMs).toBe(PINNED_SHUTDOWN_FORCE_MS)
  })

  it('keeps a window between the cancel and the force when a caller sets a longer grace', () => {
    const pinned = pinnedOptions({ shutdownGraceMs: 9_000 })
    expect(pinned.shutdownGraceMs).toBe(9_000)
    expect(pinned.shutdownForceMs).toBe(9_000 + PINNED_SHUTDOWN_FORCE_MARGIN_MS)
  })
})
