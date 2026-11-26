import { describe, expect, it } from 'vitest'
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

type Access = { owner: Owner; creator: Owner }
const OWNED: Access = { owner: OWNER, creator: OWNER }

/** A manager whose record holds `rows`; any call to Temporal fails the test. */
function managerOver(
  rows: PendingInputEntry[],
  grant = false,
  access: Access | undefined = OWNED,
  // What the caller's access is once the entries were read.
  after: { grant?: boolean; access?: Access | undefined } = {},
) {
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
        if (after.grant !== undefined) grant = after.grant
        if ('access' in after) access = after.access
        return rows
      },
    },
    approvals: {
      holdsGrant: async () => {
        grantReads.push(grant)
        return grant
      },
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
    expect(filters).toEqual([{ sessionId: SESSION, ownAnswers: false }])
  })

  // pendingInput.tools.pg.test.ts checks what that filter hides: the answers, not the approvals.
  it('asks for no answers of a session that an MCP principal owns and started, for the browser user', async () => {
    const { sessions, filters } = managerOver([entry('durable:a', 'approval')], false, { owner: GRANTED, creator: GRANTED })
    expect((await sessionPendingInput(sessions, OWNER, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:a'])
    expect(filters).toEqual([{ sessionId: SESSION, ownAnswers: true }])
  })

  it("reads a grant holder's entries with the aggregate's filter", async () => {
    const { sessions, filters, grantReads } = managerOver([entry('durable:a', 'approval')], true)
    expect((await sessionPendingInput(sessions, GRANTED, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:a'])
    expect(filters).toEqual([{ sessionId: SESSION, ownedBy: GRANTED, allApprovals: true }])
    // Before the entries are read, and again after.
    expect(grantReads).toEqual([true, true])
  })

  it("lists a session's parked answers to the browser user who started it and handed it off", async () => {
    const { sessions, filters } = managerOver([entry('durable:q', 'answer')], false, { owner: GRANTED, creator: OWNER })
    expect((await sessionPendingInput(sessions, OWNER, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:q'])
    expect(filters).toEqual([{ sessionId: SESSION, ownAnswers: false }])
  })

  it('reads no approvals, which no session has', async () => {
    const { sessions } = managerOver([entry('durable:a', 'approval')])
    sessions.approvals.listVisiblePending = async () => {
      throw new Error('a session read listed the MCP prepares')
    }
    expect((await sessionPendingInput(sessions, OWNER, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:a'])
  })

  it.each([
    ['a grant holder whose grant was revoked', GRANTED, true, OWNED, { grant: false }],
    ['a grant holder whose session was forgotten', GRANTED, true, OWNED, { access: undefined }],
    ['an owner whose session was forgotten', GRANTED, false, { owner: GRANTED, creator: GRANTED }, { access: undefined }],
    ['an owner who handed off a session it did not start', GRANTED, false, { owner: GRANTED, creator: STRANGER }, { access: { owner: OWNER, creator: STRANGER } }],
    ['the browser user whose session was forgotten', OWNER, false, OWNED, { access: undefined }],
  ])('is undefined for %s while the entries were read', async (_who, principal, grant, access, after) => {
    const { sessions, filters } = managerOver([entry('durable:a', 'approval')], grant, access, after)
    expect(await sessionPendingInput(sessions, principal, SESSION)).toBeUndefined()
    expect(filters).toHaveLength(1)
  })

  it('still lists an owner its session when its grant was revoked while the entries were read', async () => {
    const { sessions } = managerOver([entry('durable:a', 'approval')], true, { owner: GRANTED, creator: GRANTED }, { grant: false })
    expect((await sessionPendingInput(sessions, GRANTED, SESSION))?.entries.map((e) => e.id)).toEqual(['durable:a'])
  })

  it('is undefined for a principal that may not see the session, and reads nothing', async () => {
    const { sessions, filters } = managerOver([entry('durable:a', 'approval')])
    expect(await sessionPendingInput(sessions, STRANGER, SESSION)).toBeUndefined()
    expect(filters).toEqual([])
  })
})
