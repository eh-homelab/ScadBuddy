import { describe, expect, it } from 'vitest'
import { dumpThenCleanUp } from './support/workflowDump.js'

// The affinity tests' afterEach (sessionAffinity.temporal.test.ts): a failed test's dump runs
// first, and its cleanup (terminate the sessions, stop the replicas, close the pools, drop the
// schema) runs whatever the dump does.

describe('dumpThenCleanUp', () => {
  it('cleans up when the dump throws, and logs why the dump failed', async () => {
    const logged: string[] = []
    let cleaned = false
    await dumpThenCleanUp(
      () => Promise.reject(new Error('relation "ai_sessions" does not exist')),
      async () => {
        cleaned = true
      },
      { log: (text) => logged.push(text) },
    )
    expect(cleaned).toBe(true)
    expect(logged).toEqual(['the failure dump failed, cleaning up anyway: relation "ai_sessions" does not exist\n'])
  })

  it('cleans up when the dump never ends, past its deadline', async () => {
    const logged: string[] = []
    let cleaned = false
    await dumpThenCleanUp(
      () => new Promise<void>(() => {}),
      async () => {
        cleaned = true
      },
      { deadlineMs: 50, log: (text) => logged.push(text) },
    )
    expect(cleaned).toBe(true)
    expect(logged).toEqual(['the failure dump failed, cleaning up anyway: it ran past 50 ms\n'])
  })

  it('cleans up after a dump that worked, and with no dump at all; a cleanup that throws still fails the hook', async () => {
    const order: string[] = []
    await dumpThenCleanUp(
      async () => void order.push('dump'),
      async () => void order.push('cleanup'),
    )
    await dumpThenCleanUp(undefined, async () => void order.push('cleanup'))
    expect(order).toEqual(['dump', 'cleanup', 'cleanup'])
    await expect(dumpThenCleanUp(undefined, () => Promise.reject(new Error('drop failed')))).rejects.toThrow('drop failed')
  })
})
