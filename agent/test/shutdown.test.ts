import { describe, expect, it } from 'vitest'
import { shutdown } from '../src/shutdown.js'

describe('shutdown', () => {
  it('waits for the server to drain before closing the database', async () => {
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
      closeDatabase: async () => {
        order.push('db.close')
      },
    })
    await new Promise((r) => setTimeout(r, 10))
    expect(order).toEqual(['server.close'])
    finishRequests()
    expect(await done).toBe('clean')
    expect(order).toEqual(['server.close', 'server.drained', 'db.close'])
  })

  it('gives up on a server that never drains, and still closes the database', async () => {
    let dbClosed = false
    const result = await shutdown({
      closeServer: () => new Promise<void>(() => {}),
      closeDatabase: async () => {
        dbClosed = true
      },
      timeoutMs: 20,
    })
    expect(result).toBe('timed out')
    expect(dbClosed).toBe(true)
  })

  it('works without a database and tolerates a failing close', async () => {
    expect(await shutdown({ closeServer: async () => {} })).toBe('clean')
    expect(
      await shutdown({
        closeServer: () => Promise.reject(new Error('Server is not running.')),
        closeDatabase: () => Promise.reject(new Error('boom')),
      }),
    ).toBe('clean')
  })
})
