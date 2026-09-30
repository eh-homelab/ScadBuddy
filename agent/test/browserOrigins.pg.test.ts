import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { loadApprovedOrigins, rememberApprovedOrigin } from '../src/harness/browserOrigins.js'
import { TOOL_PREFIX } from '../src/harness/headlessBrowser.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// The per-session record of origins a human let the headless browser open
// (#349, src/harness/browserOrigins.ts, `ai_browser_origins`), against the
// agent's real schema. The whole flow in a session turn is
// headlessBrowser.session.e2e.test.ts.

const DOCS = 'https://docs.example'

describe.skipIf(!TEST_DATABASE_URL)(`approved browser origins${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
  })
  afterEach(async () => {
    await drop()
  })

  /** A session and an approval of a navigation in it; approved and used unless told otherwise. */
  async function navigationApproval(state: 'used' | 'approved' | 'pending' = 'used') {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    // A live turn parked on it, as in a real turn (headlessGrants.pg.test.ts).
    const [{ turn_id: turnId } = { turn_id: '' }] = await db.sql<{ turn_id: string }[]>`
      UPDATE ai_sessions SET turn_id = gen_random_uuid(), lease_until = now() + interval '1 minute'
      WHERE id = ${session.id} RETURNING turn_id`
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId,
      toolUseId: 'toolu_1',
      tool: `${TOOL_PREFIX}browser_navigate`,
      input: { url: `${DOCS}/manual` },
      tier: 'outward',
      requestedBy: agentA,
    })
    if (state !== 'pending') await m.approvals.decide(browser, approval.id, true)
    if (state === 'used') expect(await m.approvals.consumeById(approval.id)).toBeDefined()
    return { session, approval }
  }

  it('remembers an origin under the approved, used approval, once, for that session only', async () => {
    const { session, approval } = await navigationApproval()
    expect(await loadApprovedOrigins(db.sql, session.id)).toEqual([])
    await rememberApprovedOrigin(db.sql, session.id, DOCS, approval.id)
    await rememberApprovedOrigin(db.sql, session.id, DOCS, approval.id)
    expect(await loadApprovedOrigins(db.sql, session.id)).toEqual([DOCS])
    const other = await navigationApproval()
    expect(await loadApprovedOrigins(db.sql, other.session.id)).toEqual([])
  })

  it('refuses without an approval, with one not yet used or undecided, or with another session’s', async () => {
    const pending = await navigationApproval('pending')
    await expect(rememberApprovedOrigin(db.sql, pending.session.id, DOCS, pending.approval.id)).rejects.toThrow(/not an approved, used/)
    const unused = await navigationApproval('approved')
    await expect(rememberApprovedOrigin(db.sql, unused.session.id, DOCS, unused.approval.id)).rejects.toThrow(/not an approved, used/)
    const used = await navigationApproval()
    await expect(rememberApprovedOrigin(db.sql, pending.session.id, DOCS, used.approval.id)).rejects.toThrow(/not an approved, used/)
    await expect(rememberApprovedOrigin(db.sql, used.session.id, DOCS, undefined)).rejects.toThrow(/no approval/)
    expect(await loadApprovedOrigins(db.sql, pending.session.id)).toEqual([])
  })

  it('stores normalised origins only', async () => {
    const { session, approval } = await navigationApproval()
    await expect(rememberApprovedOrigin(db.sql, session.id, `${DOCS}/manual`, approval.id)).rejects.toThrow(/check constraint/)
  })
})
