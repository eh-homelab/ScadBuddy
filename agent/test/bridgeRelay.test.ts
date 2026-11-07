import { TimeoutFailure, WorkflowFailedError } from '@temporalio/client'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { hostTabs, type RelayOwner, type SealingClient, TemporalTabRelay } from '../src/bridge/relay.js'
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

/** A sealing client whose workflows start and then fail with `err`. */
function failingWorkflowClient(err: Error = new Error('the owner failed')): SealingClient {
  const handle = { workflowId: 'bridge-x', result: () => Promise.reject(err), cancel: async () => {} }
  return { client: { workflow: { start: async () => handle } } as never, dropKey: async () => {} }
}

/** What the Temporal client's `result()` rejects with when a workflow hits its execution timeout. */
const executionTimeout = () =>
  new WorkflowFailedError('Workflow execution timed out', new TimeoutFailure('Workflow execution timed out', undefined, 'START_TO_CLOSE'), 'TIMEOUT')

const owner = (tabs: string[]): RelayOwner => ({ tabs: () => tabs, run: async () => ({ op: 'gone' }) })

describe('TemporalTabRelay', () => {
  it('claims no tab until its host worker polls, and claims the ones it holds once it does', async () => {
    const owners = memoryOwners()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: refusingClient().sealing, log: () => {} })
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
    const relay = new TemporalTabRelay({
      owners,
      hostQueue: 'agent-tools.a',
      client: refusingClient().sealing,
      refreshMs: 20,
      ready: async () => up,
      log: () => {},
    })
    relay.serve(owner(['tab-1']))
    relay.startHosting() // the database is down: its first claim is not written
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.size).toBe(0)
    up = true
    await vi.waitFor(() => expect(owners.rows.get('tab-1')).toBe('agent-tools.a'))
    relay.close()
  })

  it('claims no tab without a sealing client: a replica with no KEK could not decode the calls sent to it (#4949)', async () => {
    const owners = memoryOwners()
    const logged: string[] = []
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: (m) => logged.push(m) })
    relay.serve(owner(['tab-0']))
    relay.startHosting()
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.size).toBe(0)
    expect(logged.join('\n')).toMatch(/no sealing client/)
    relay.close()
  })

  it('logs the refusal once, however often hosting is asked to start (#5421)', async () => {
    const owners = memoryOwners()
    const logged: string[] = []
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: (m) => logged.push(m) })
    relay.startHosting()
    relay.startHosting()
    expect(logged).toHaveLength(1)
    relay.close()
  })

  it('hostTabs sets the sealing client before hosting starts, so a polling replica claims its tabs (#5421)', async () => {
    const owners = memoryOwners()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: () => {} })
    relay.serve(owner(['tab-0']))
    // The host worker already polls, as main.ts finds it once the worker runs.
    const stop = hostTabs(relay, { sealing: refusingClient().sealing, worker: { state: () => 'ok' } })
    // Cleaned up even when an assertion fails, so no poll outlives the test (#5439).
    onTestFinished(() => {
      stop()
      relay.close()
    })
    relay.attached('tab-1')
    await settle()
    expect(owners.rows).toEqual(new Map([['tab-0', 'agent-tools.a'], ['tab-1', 'agent-tools.a']]))
  })

  it('hostTabs waits for the host worker to poll, and without a sealing client claims nothing and logs once (#5421)', async () => {
    const owners = memoryOwners()
    const logged: string[] = []
    let state = 'connecting'
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: (m) => logged.push(m) })
    relay.serve(owner(['tab-0']))
    const stop = hostTabs(relay, { sealing: undefined, worker: { state: () => state }, pollMs: 5 })
    onTestFinished(() => {
      stop()
      relay.close()
    })
    await settle()
    expect(logged).toEqual([])
    state = 'ok'
    await vi.waitFor(() => expect(logged).toHaveLength(1))
    await new Promise((r) => setTimeout(r, 30))
    expect(logged).toHaveLength(1)
    expect(logged[0]).toMatch(/no sealing client .* no tab is claimed here/)
    expect(owners.rows.size).toBe(0)
  })

  it('hostTabs with a client and a worker still connecting claims nothing until the worker is ok (#4806, #5432)', async () => {
    const owners = memoryOwners()
    let state = 'connecting'
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: () => {} })
    relay.serve(owner(['tab-0']))
    const stop = hostTabs(relay, { sealing: refusingClient().sealing, worker: { state: () => state }, pollMs: 5 })
    onTestFinished(() => {
      stop()
      relay.close()
    })
    // Several polls while the worker connects: none claims.
    await new Promise((r) => setTimeout(r, 30))
    expect(owners.rows.size).toBe(0)
    state = 'ok'
    await vi.waitFor(() => expect(owners.rows).toEqual(new Map([['tab-0', 'agent-tools.a']])))
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
    expect(logged).toEqual(['bridge relay: the owner failed'])
  })

  it('logs a workflow execution timeout apart from an owner failure (#4950)', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-1', 'agent-tools.b')
    const logged: string[] = []
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: failingWorkflowClient(executionTimeout()), log: (m) => logged.push(m) })
    expect(await relay.forward('tab-1', call, options())).toMatchObject({ op: 'call', outcome: { ok: false, error: { code: 'no_answer' } } })
    expect(logged).toHaveLength(1)
    expect(logged[0]).toMatch(/tab-1 on agent-tools\.b timed out as a workflow \(rollout\?/)
  })
})
