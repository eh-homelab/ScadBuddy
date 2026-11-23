import { describe, expect, it } from 'vitest'
import { shutdown } from '../src/shutdown.js'

describe('shutdown', () => {
  it('waits for the server to drain before the last step', async () => {
    const order: string[] = []
    let finishRequests!: () => void
    const drained = new Promise<void>((resolve) => {
      finishRequests = resolve
    })
    const done = shutdown({
      closeServer: async () => {
        order.push('server.close')
        await drained
        order.push('server.drained')
      },
      closeLast: async () => {
        order.push('db.close')
      },
    })
    await new Promise((r) => setTimeout(r, 10))
    expect(order).toEqual(['server.close'])
    finishRequests()
    expect(await done).toBe('clean')
    expect(order).toEqual(['server.close', 'server.drained', 'db.close'])
  })

  it('gives up on a server that never drains, and still runs the last step', async () => {
    let dbClosed = false
    const result = await shutdown({
      closeServer: () => new Promise<void>(() => {}),
      closeLast: async () => {
        dbClosed = true
      },
      timeoutMs: 20,
    })
    expect(result).toBe('timed out')
    expect(dbClosed).toBe(true)
  })

  it('works without a last step and tolerates a failing one', async () => {
    expect(await shutdown({ closeServer: async () => {} })).toBe('clean')
    expect(
      await shutdown({
        closeServer: () => Promise.reject(new Error('Server is not running.')),
        closeLast: () => Promise.reject(new Error('boom')),
      }),
    ).toBe('clean')
  })

  it("never holds the server's close behind a slow step of closeLast (#5881 review)", async () => {
    const order: string[] = []
    let endPinned!: () => void
    const pinnedStopped = new Promise<void>((resolve) => (endPinned = resolve))
    const done = shutdown({
      closeServer: async () => void order.push('server closed'),
      closeLast: async () => {
        await pinnedStopped
        order.push('last step done')
      },
      timeoutMs: 1_000,
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(order).toEqual(['server closed'])
    endPinned()
    expect(await done).toBe('clean')
    expect(order).toEqual(['server closed', 'last step done'])
  })
})
