import { describe, expect, it } from 'vitest'
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
