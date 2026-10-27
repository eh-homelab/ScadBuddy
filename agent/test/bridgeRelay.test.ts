import { describe, expect, it, vi } from 'vitest'
import type { LegacyTabRelay } from '../src/bridge/pgRelay.js'
import { type SealingClient, TemporalTabRelay } from '../src/bridge/relay.js'
import type { TabOwnerStore } from '../src/bridge/tabOwners.js'

// TemporalTabRelay's own rules, without Temporal or Postgres (#4780 review). The calls
// across replicas are bridgeRelay.temporal.test.ts.

function memoryOwners(): TabOwnerStore & { rows: Map<string, string> } {
  const rows = new Map<string, string>()
  return {
    rows,
    claim: async (tab, queue) => void rows.set(tab, queue),
    release: async (tab, queue) => void (rows.get(tab) === queue && rows.delete(tab)),
    releaseAll: async (queue) => {
      for (const [tab, q] of rows) if (q === queue) rows.delete(tab)
    },
    ownerOf: async (tab) => rows.get(tab),
  }
}

const settle = () => new Promise((r) => setTimeout(r, 10))
const call = { op: 'call' as const, tool: 'snapshot', args: {}, timeoutMs: 1_000 }

/** A sealing client whose every workflow start fails, as a Temporal that refuses it. */
function failingClient(): { sealing: SealingClient; start: ReturnType<typeof vi.fn> } {
  const start = vi.fn(() => Promise.reject(new Error('14 UNAVAILABLE: no connection')))
  return { sealing: { client: { workflow: { start } } as never, dropKey: async () => {} }, start }
}

describe('TemporalTabRelay', () => {
  it('claims no tab while its host worker does not serve bridge.tab', async () => {
    const owners = memoryOwners()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', log: () => {} })
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.size).toBe(0)
    relay.hostActivities()
    relay.attached('tab-1')
    await settle()
    expect(owners.rows.get('tab-1')).toBe('agent-tools.a')
    relay.detached('tab-1')
    await settle()
    expect(owners.rows.size).toBe(0)
  })

  it('answers no_answer, and logs, when Temporal fails the call: the tool never throws', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-1', 'agent-tools.b')
    const logged: string[] = []
    const { sealing, start } = failingClient()
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: sealing, log: (m) => logged.push(m) })
    const answer = await relay.forward('tab-1', call, { signal: new AbortController().signal, timeoutMs: 1_000 })
    expect(answer).toMatchObject({ op: 'call', outcome: { ok: false, error: { code: 'no_answer' } } })
    expect(start).toHaveBeenCalledOnce()
    expect(logged.join('\n')).toMatch(/UNAVAILABLE/)
    // A status read answers "not connected".
    expect(await relay.forward('tab-1', { op: 'status' }, { signal: new AbortController().signal, timeoutMs: 1_000 })).toBeUndefined()
  })

  it('takes the old relay when Temporal fails the call, while it is there (#4868)', async () => {
    const owners = memoryOwners()
    owners.rows.set('tab-1', 'agent-tools.b')
    const fallback: LegacyTabRelay = {
      serve: () => {},
      forward: vi.fn(async () => ({ op: 'call' as const, outcome: { ok: true as const, result: 'from the old relay' } })),
    }
    const relay = new TemporalTabRelay({ owners, hostQueue: 'agent-tools.a', client: failingClient().sealing, fallback, log: () => {} })
    expect(await relay.forward('tab-1', call, { signal: new AbortController().signal, timeoutMs: 1_000 })).toEqual({
      op: 'call',
      outcome: { ok: true, result: 'from the old relay' },
    })
    expect(fallback.forward).toHaveBeenCalledOnce()
  })
})
