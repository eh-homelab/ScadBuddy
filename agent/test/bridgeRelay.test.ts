import { describe, expect, it, vi } from 'vitest'
import { type RelayOwner, type SealingClient, TemporalTabRelay } from '../src/bridge/relay.js'
import type { TabOwnerStore } from '../src/bridge/tabOwners.js'

// TemporalTabRelay's own rules, without Temporal or Postgres (#4780, #4806 review). The
// calls across replicas are bridgeRelay.temporal.test.ts; the Postgres store's own rules,
// bridgeStores.pg.test.ts.

function memoryOwners(): TabOwnerStore & { rows: Map<string, string> } {
  const rows = new Map<string, string>()
  return {
    rows,
    claim: async (tab, queue) => void rows.set(tab, queue),
    refresh: async (tabs, queue) => {
      for (const tab of tabs) if (!rows.has(tab)) rows.set(tab, queue)
    },
    release: async (tab, queue) => void (rows.get(tab) === queue && rows.delete(tab)),
    releaseAll: async (queue) => {
      for (const [tab, q] of rows) if (q === queue) rows.delete(tab)
    },
    ownerOf: async (tab) => {
      const queue = rows.get(tab)
      return queue === undefined ? undefined : { queue, claimedAt: '2026-10-27 12:00:00.000001+00' }
    },
  }
}

const settle = () => new Promise((r) => setTimeout(r, 10))
const call = { op: 'call' as const, tool: 'click', args: {}, timeoutMs: 1_000 }
const options = () => ({ signal: new AbortController().signal, timeoutMs: 1_000 })

/** A sealing client whose every workflow start fails, as a Temporal that refuses it. */
function refusingClient(): { sealing: SealingClient; start: ReturnType<typeof vi.fn> } {
  const start = vi.fn(() => Promise.reject(new Error('14 UNAVAILABLE: no connection')))
  return { sealing: { client: { workflow: { start } } as never, dropKey: async () => {} }, start }
}

/** A sealing client whose workflows start and then fail, as one timed out during a rollout. */
function failingWorkflowClient(): SealingClient {
  const handle = { workflowId: 'bridge-x', result: () => Promise.reject(new Error('workflow execution timed out')), cancel: async () => {} }
  return { client: { workflow: { start: async () => handle } } as never, dropKey: async () => {} }
}

const owner = (tabs: string[]): RelayOwner => ({ tabs: () => tabs, run: async () => ({ op: 'gone' }) })

describe('TemporalTabRelay', () => {
  it('claims no tab until its host worker polls, and claims the ones it holds once it does', async () => {
    const owners = memoryOwners()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: () => {} })
    relay.serve(owner(['tab-0']))
    // The activities are built for a worker that has not started, or failed to.
    relay.hostActivities()
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.size).toBe(0)
    relay.startHosting()
    await settle()
    expect(owners.rows.get('tab-0')).toBe('agent-tools.a') // held before the worker ran
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.get('tab-1')).toBe('agent-tools.a')
    relay.detached('tab-1')
    await settle()
    expect([...owners.rows.keys()]).toEqual(['tab-0'])
    relay.close()
  })

  it('claims its live tabs again on a timer: a claim lost while the database was down comes back', async () => {
    const owners = memoryOwners()
    let up = false
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', refreshMs: 20, ready: async () => up, log: () => {} })
    relay.serve(owner(['tab-1']))
    relay.startHosting() // the database is down: its first claim is not written
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.size).toBe(0)
    up = true
    await vi.waitFor(() => expect(owners.rows.get('tab-1')).toBe('agent-tools.a'))
    relay.close()
  })

  it('answers no_answer, and logs, when Temporal refuses the start: the tool never throws', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-1', 'agent-tools.b')
    const logged: string[] = []
    const { sealing, start } = refusingClient()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: sealing, log: (m) => logged.push(m) })
    expect(await relay.forward('tab-1', call, options())).toMatchObject({ op: 'call', outcome: { ok: false, error: { code: 'no_answer' } } })
    expect(start).toHaveBeenCalledOnce()
    expect(logged.join('\n')).toMatch(/UNAVAILABLE/)
    // A status read answers "not connected".
    expect(await relay.forward('tab-1', { op: 'status' }, options())).toBeUndefined()
  })

  it('with no sealed Temporal client, a tab on another replica is refused (fail closed) and a status read is "not connected"', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-1', 'agent-tools.b')
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: () => {} })
    expect(await relay.forward('tab-1', call, options())).toMatchObject({
      op: 'call',
      outcome: { ok: false, error: { code: 'no_browser', message: expect.stringMatching(/SCADBUDDY_SECRET_KEY_FILE/) } },
    })
    expect(await relay.forward('tab-1', { op: 'status' }, options())).toBeUndefined()
  })

  it('with no sealed Temporal client, a tab no replica holds (or this one holds) is the plain "not connected"', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-here', 'agent-tools.a')
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: () => {} })
    expect(await relay.forward('tab-none', call, options())).toBeUndefined()
    expect(await relay.forward('tab-here', call, options())).toBeUndefined()
  })

  it("answers no_answer, and logs, when the owner's row cannot be read: the tool never throws", async () => {
    const owners = memoryOwners()
    owners.ownerOf = () => Promise.reject(new Error('connection refused'))
    const logged: string[] = []
    const { sealing, start } = refusingClient()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: sealing, log: (m) => logged.push(m) })
    expect(await relay.forward('tab-1', call, options())).toMatchObject({ op: 'call', outcome: { ok: false, error: { code: 'no_answer' } } })
    expect(await relay.forward('tab-1', { op: 'status' }, options())).toBeUndefined()
    expect(start).not.toHaveBeenCalled()
    expect(logged.join('\n')).toMatch(/connection refused/)
  })

  it('answers no_answer, once, when its workflow started and then failed: a click must not run twice', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-1', 'agent-tools.b')
    const logged: string[] = []
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: failingWorkflowClient(), log: (m) => logged.push(m) })
    expect(await relay.forward('tab-1', call, options())).toMatchObject({ op: 'call', outcome: { ok: false, error: { code: 'no_answer' } } })
    expect(logged.join('\n')).toMatch(/timed out/)
  })
})
