import type { Client } from '@temporalio/client'
import { WorkflowNotFoundError } from '@temporalio/common'
import { describe, expect, it } from 'vitest'
import { DurableGate, DurableUnavailable } from '../src/gate/durable.js'
import type { PendingInputEntry } from '../src/gate/projection.js'
import { sessionPendingInput } from '../src/routes/pendingInput.js'
import type { SessionManager } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'

// A grant holder's read of a durable session it does not own (#2863): its approvals, none
// of a flow run's, found with one query for every entry rather than one each.

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const OWNER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const GRANTED: Owner = { kind: 'bearer', id: 'token:granted', label: 'Another agent' }

const entry = (id: string, kind: 'approval' | 'question'): PendingInputEntry => ({ id, kind }) as unknown as PendingInputEntry

describe('sessionPendingInput for a grant holder', () => {
  it("reads which entries are a flow run's in one query, and hides those and every question", async () => {
    const lookups: string[][] = []
    const sessions = {
      projection: {
        session: async () => ({ mode: 'durable', owner: OWNER, creator: OWNER }),
        inRun: () => Promise.reject(new Error('one query per entry')),
        inRunOf: async (ids: readonly string[]) => {
          lookups.push([...ids])
          return new Set(['b'])
        },
        decidedOf: async () => new Set<string>(),
      },
      approvals: { holdsGrant: async () => true },
      durable: { pendingInput: async () => [entry('a', 'approval'), entry('b', 'approval'), entry('q', 'question'), entry('c', 'approval')] },
    } as unknown as SessionManager
    const page = await sessionPendingInput(sessions, GRANTED, SESSION)
    expect(page?.entries.map((e) => e.id)).toEqual(['a', 'c'])
    expect(lookups).toEqual([['a', 'b', 'c']])
  })
})

// A closed or missing workflow has no pending input (#7311, a regression from #7036: archive
// and forget end the run, and past Temporal's retention a query of it is not found).
describe("DurableGate.pendingInput and a session's workflow", () => {
  const parked = [entry('a', 'approval')]
  function gateOver(describe: () => Promise<unknown>) {
    const queried: (string | undefined)[] = []
    const client = {
      withDeadline: (_deadline: number, fn: () => Promise<unknown>) => fn(),
      workflow: {
        getHandle: (_id: string, runId?: string) => ({
          describe,
          query: async () => {
            queried.push(runId)
            return parked
          },
        }),
      },
    } as unknown as Client
    return { gate: new DurableGate(client), queried }
  }

  it("lists an open run's input, from the run it described", async () => {
    const { gate, queried } = gateOver(async () => ({ runId: 'run-1', status: { name: 'RUNNING' } }))
    expect(await gate.pendingInput(SESSION)).toEqual(parked)
    expect(queried).toEqual(['run-1'])
  })

  it.each(['COMPLETED', 'TERMINATED', 'FAILED', 'TIMED_OUT', 'CANCELLED'])('lists none for a %s run, and does not query it', async (name) => {
    const { gate, queried } = gateOver(async () => ({ runId: 'run-1', status: { name } }))
    expect(await gate.pendingInput(SESSION)).toEqual([])
    expect(queried).toEqual([])
  })

  it('lists none for a workflow that is not found', async () => {
    const { gate } = gateOver(() => Promise.reject(new WorkflowNotFoundError('not found', `session-${SESSION}`, undefined)))
    expect(await gate.pendingInput(SESSION)).toEqual([])
  })

  it('is unavailable, not empty, while Temporal does not answer', async () => {
    const { gate } = gateOver(() => Promise.reject(new Error('14 UNAVAILABLE: connect ECONNREFUSED')))
    await expect(gate.pendingInput(SESSION)).rejects.toBeInstanceOf(DurableUnavailable)
  })
})

describe('the one-session read over a closed workflow', () => {
  it('answers an empty page, not a 404, once the gate lists nothing', async () => {
    const sessions = {
      projection: {
        session: async () => ({ mode: 'durable', owner: OWNER, creator: OWNER }),
        decidedOf: async () => new Set<string>(),
      },
      approvals: { holdsGrant: async () => false },
      durable: { pendingInput: async () => [] },
    } as unknown as SessionManager
    expect(await sessionPendingInput(sessions, OWNER, SESSION)).toEqual({ entries: [], summaries_truncated: false })
  })
})
