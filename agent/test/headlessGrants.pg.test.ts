import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { AUTHORIZE_TOOL_NAME } from '../src/harness/headlessBrowser.js'
import { recordGrant } from '../src/harness/headlessGrants.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// The grant the headless browser needs for one outward request (#349, spec
// §5.3; src/harness/headlessGrants.ts), against the agent's real schema, and
// the backend's GRANT_SQL (backend/scadbuddy/api/agent_actor.py) run against
// that same schema, so the two services cannot drift apart.

const GATE = fileURLToPath(new URL('../../backend/scadbuddy/api/agent_actor.py', import.meta.url))

/** The backend's GRANT_SQL, with its `%(name)s` parameters turned into `$n`. */
function backendGrantSql(): { text: string; order: string[] } {
  const source = readFileSync(GATE, 'utf8')
  const raw = /^GRANT_SQL = """([\s\S]*?)"""/m.exec(source)?.[1]
  if (!raw) throw new Error('GRANT_SQL not found in agent_actor.py')
  const order: string[] = []
  const text = raw.replace(/%\((\w+)\)s/g, (_m, name: string) => {
    if (!order.includes(name)) order.push(name)
    return `$${order.indexOf(name) + 1}`
  })
  return { text, order }
}

const RUN = '/api/v1/print/outputs/out-1/run'
const INPUT = { method: 'POST' as const, path: RUN }

describe.skipIf(!TEST_DATABASE_URL)(`headless-browser grants${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
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

  /** A session with a live turn, and an approval of the authorize tool for `input` in it. */
  async function approvedInTurn(input: Record<string, unknown> = INPUT, approve = true) {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const [{ turn_id: turnId } = { turn_id: '' }] = await db.sql<{ turn_id: string }[]>`
      UPDATE ai_sessions SET turn_id = gen_random_uuid(), lease_until = now() + interval '1 minute'
      WHERE id = ${session.id} RETURNING turn_id`
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId,
      toolUseId: 'toolu_1',
      tool: AUTHORIZE_TOOL_NAME,
      input,
      tier: 'outward',
      requestedBy: agentA,
    })
    if (approve) {
      await m.approvals.decide(browser, approval.id, true)
      expect(await m.approvals.consumeById(approval.id)).toBeDefined()
    }
    const context = { sql: db.sql, sessionId: session.id, turnId, hash: m.approvals.hash.bind(m.approvals) }
    return { session, turnId, approval, context }
  }

  async function backendUses(session: string, method: string, path: string): Promise<boolean> {
    const { text, order } = backendGrantSql()
    const values: Record<string, string> = { session, method, path }
    const rows = await db.sql.unsafe(text, order.map((name) => values[name]!))
    return rows.length === 1
  }

  it('turns an approved, consumed call into a grant the backend uses once for that exact request', async () => {
    const { session, context } = await approvedInTurn()
    const grant = await recordGrant(context, INPUT)
    expect(grant.ok).toBe(true)
    expect(await backendUses(session.id, 'POST', `${RUN}/x`)).toBe(false)
    expect(await backendUses(session.id, 'PUT', RUN)).toBe(false)
    expect(await backendUses(session.id, 'POST', RUN)).toBe(true)
    expect(await backendUses(session.id, 'POST', RUN)).toBe(false)
    // One approval, one grant.
    expect(await recordGrant(context, INPUT)).toMatchObject({ ok: false })
  })

  it('matches a trailing slash the way the gate allow-list does, and uses the grant once', async () => {
    const { session, context } = await approvedInTurn()
    expect((await recordGrant(context, INPUT)).ok).toBe(true)
    expect(await backendUses(session.id, 'POST', `${RUN}//x`)).toBe(false)
    expect(await backendUses(session.id, 'POST', `${RUN}/`)).toBe(true)
    expect(await backendUses(session.id, 'POST', RUN)).toBe(false)
  })

  it('refuses a grant without an approval, or for another input than the approved one', async () => {
    const pending = await approvedInTurn(INPUT, false)
    expect(await recordGrant(pending.context, INPUT)).toMatchObject({ ok: false })
    const other = await approvedInTurn()
    expect(await recordGrant(other.context, { method: 'DELETE', path: RUN })).toMatchObject({ ok: false })
  })

  it('refuses an approval from another turn', async () => {
    const { context } = await approvedInTurn()
    expect(
      await recordGrant({ ...context, turnId: '00000000-0000-4000-8000-000000000000' }, INPUT),
    ).toMatchObject({ ok: false })
  })

  it('stops the backend using a grant once its turn is over', async () => {
    const { session, context } = await approvedInTurn()
    expect((await recordGrant(context, INPUT)).ok).toBe(true)
    await db.sql`UPDATE ai_sessions SET turn_id = gen_random_uuid() WHERE id = ${session.id}`
    expect(await backendUses(session.id, 'POST', RUN)).toBe(false)
  })

  it('stops the backend using an expired grant', async () => {
    const { session, context } = await approvedInTurn()
    expect((await recordGrant({ ...context, ttlSeconds: -1 }, INPUT)).ok).toBe(true)
    expect(await backendUses(session.id, 'POST', RUN)).toBe(false)
  })

  it('refuses a path the migration does not allow', async () => {
    const { context } = await approvedInTurn({ method: 'POST', path: '/api/v1/../admin' })
    await expect(recordGrant(context, { method: 'POST', path: '/api/v1/../admin' })).rejects.toThrow()
  })
})
