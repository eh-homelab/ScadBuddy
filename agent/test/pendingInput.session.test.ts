import { describe, expect, it } from 'vitest'
import { ApprovalError } from '../src/approvals/service.js'
import type { PendingInputEntry, ProjectionFilter } from '../src/gate/projection.js'
import { sessionPendingInput } from '../src/routes/pendingInput.js'
import type { SessionManager } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'

// One session's pending input is the aggregate read narrowed to it: the record
// (`ai_pending_input`) through the projection, never a Query of the session's workflow
// (#7311: a closed workflow is archived after Temporal's retention and cannot be queried).

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const OWNER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const GRANTED: Owner = { kind: 'bearer', id: 'token:granted', label: 'Another agent' }
const STRANGER: Owner = { kind: 'bearer', id: 'token:stranger', label: 'Someone' }

const entry = (id: string, kind: 'approval' | 'answer'): PendingInputEntry =>
  ({ id, kind, session_id: SESSION, created_at: '2026-11-23T00:00:00.000Z' }) as unknown as PendingInputEntry

/** A manager whose record holds `rows`; any call to Temporal fails the test. */
function managerOver(rows: PendingInputEntry[], grant = false, access = { owner: OWNER, creator: OWNER }) {
  const filters: ProjectionFilter[] = []
  const grantReads: boolean[] = []
  const temporal = () => {
    throw new Error('the per-session read asked Temporal')
  }
  const sessions = {
    projection: {
      session: async () => access,
      entries: async (filter: ProjectionFilter) => {
        filters.push(filter)
        return rows
      },
    },
    approvals: {
      holdsGrant: async () => {
        grantReads.push(grant)
        return grant
      },
      list: async () => [],
    },
    questions: { listPending: async () => ({ questions: [], summariesTruncated: false }) },
    queue: { ownEntries: async () => [] },
    durable: new Proxy({}, { get: temporal }),
  } as unknown as SessionManager
  return { sessions, filters, grantReads }
}

describe("one session's pending input", () => {
  it('lists none for an archived session whose workflow is closed or gone, with no Temporal call', async () => {
    const { sessions } = managerOver([])
    expect(await sessionPendingInput(sessions, OWNER, SESSION)).toEqual({ entries: [], summaries_truncated: false })
  })

  it("lists an undecided row of the session's record", async () => {
    const { sessions, filters } = managerOver([entry('durable:a', 'approval')])
    const page = await sessionPendingInput(sessions, OWNER, SESSION)
    expect(page?.entries.map((e) => e.id)).toEqual(['durable:a'])
    expect(filters).toEqual([{ sessionId: SESSION, ownAnswers: true }])
  })

  it("reads a grant holder's entries with the aggregate's filter", async () => {
    const { sessions, filters, grantReads } = managerOver([entry('durable:a', 'approval')], true)
    expect((await sessionPendingInput(sessions, GRANTED, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:a'])
    expect(filters).toEqual([{ sessionId: SESSION, ownedBy: GRANTED, allApprovals: true }])
    expect(grantReads).toEqual([true])
  })

  it("lists a session's parked answers to the browser user who started it and handed it off", async () => {
    const { sessions, filters } = managerOver([entry('durable:q', 'answer')], false, { owner: GRANTED, creator: OWNER })
    expect((await sessionPendingInput(sessions, OWNER, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:q'])
    expect(filters).toEqual([{ sessionId: SESSION, ownAnswers: false }])
  })

  it("is undefined, not an error, when the approvals' own check finds no session", async () => {
    const { sessions } = managerOver([entry('durable:a', 'approval')])
    sessions.approvals.list = async () => {
      throw new ApprovalError('not_found', `no session ${SESSION}`)
    }
    expect(await sessionPendingInput(sessions, OWNER, SESSION)).toBeUndefined()
  })

  it('lets any other failure of the approvals through', async () => {
    const { sessions } = managerOver([])
    sessions.approvals.list = async () => {
      throw new Error('the API does not answer')
    }
    await expect(sessionPendingInput(sessions, OWNER, SESSION)).rejects.toThrow('the API does not answer')
  })

  it('is undefined for a principal that may not see the session, and reads nothing', async () => {
    const { sessions, filters } = managerOver([entry('durable:a', 'approval')])
    expect(await sessionPendingInput(sessions, STRANGER, SESSION)).toBeUndefined()
    expect(filters).toEqual([])
  })
})
