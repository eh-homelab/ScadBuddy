import { fixedCredentials } from './support/fixedCredentials.js'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { forgetSubject } from '../src/sessions/forget.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import { SealError } from '../src/secrets.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// A fork is copy-on-write for the transcript (plan 2026-10-13 Step 6.5, decision Q8, #4212):
// the child records where it cut its parent's transcript, and the store reads the parent's
// entries through that cut, then the child's own. Forgetting the parent copies the range each
// fork references into it, under the fork's own key. Run end to end: the real SDK and its
// bundled Claude Code against the fake Anthropic endpoint, AgentSession turns on Temporal.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

/** Every text the fake was sent in a request's messages. */
function conversation(request: RecordedRequest | undefined): string {
  return JSON.stringify(request?.body?.messages ?? [])
}

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(`forks are copy-on-write${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
  let fake: FakeAnthropic
  let db: Database
  let schema: string
  let url: string
  let drop: () => Promise<void>
  const pools: Database[] = []

  beforeEach(async () => {
    // Every reply names what it answers, so a request's messages say which turns it holds.
    fake = await startFakeAnthropic((r: RecordedRequest): Reply => {
      const said = conversation(r)
      const last = [...said.matchAll(/turn:([a-z]+)/g)].at(-1)?.[1] ?? 'none'
      return { text: `reply to ${last}` }
    })
    ;({ db, schema, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await stopManagers()
    await fake.close()
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  /** A replica: a fresh state dir and its own pool on the shared schema. */
  async function replica(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(url, { searchPath: schema })
    pools.push(pool)
    return manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-fork-cow-test-token' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
    })
  }

  async function turn(m: SessionManager, id: string, word: string): Promise<string> {
    const done = await (await m.send(id, browser, `turn:${word}`)).done
    expect(done).toMatchObject({ kind: 'result', subtype: 'success' })
    return conversation(fake.messageCalls().at(-1))
  }

  async function started(m: SessionManager, word: string): Promise<string> {
    const { session, turn: first } = await m.start(browser, { origin: 'chat', prompt: `turn:${word}` })
    expect(await first!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    return session.id
  }

  const ownRows = async (id: string) =>
    db.sql<{ uuid: string | null; inherited: boolean }[]>`
      SELECT uuid, inherited FROM ai_session_entries WHERE session_id = ${id} ORDER BY id`

  const link = async (id: string) =>
    (await db.sql<{ parent: string | null; cut: number | null }[]>`
      SELECT fork_parent_id::text AS parent, fork_cut AS cut FROM ai_sessions WHERE id = ${id}`)[0]

  it('a fork writes no entry rows until its first turn', async () => {
    const m = await replica()
    const parent = await started(m, 'box')
    const parentUuids = new Set((await ownRows(parent)).map((r) => r.uuid).filter((u) => u !== null))
    const child = await m.fork(parent, browser)

    expect(await ownRows(child.id)).toEqual([])
    expect(await link(child.id)).toEqual({ parent, cut: (await m.store.load({ projectKey: '', sessionId: parent }))!.length })
    // The store reads the parent's through the cut: the child resumes, and loads what its parent did.
    expect(await m.store.exists(child.id)).toBe(true)
    expect(await m.store.load({ projectKey: '', sessionId: child.id })).toEqual(await m.store.load({ projectKey: '', sessionId: parent }))

    const sent = await turn(m, child.id, 'left')
    expect(sent).toContain('turn:box')
    expect(sent).toContain('turn:left')
    // Its first turn writes only what it adds: none of the parent's entries again.
    const rows = await ownRows(child.id)
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.filter((r) => r.uuid !== null && parentUuids.has(r.uuid))).toEqual([])
  }, 90_000)

  it("the child's first turn's model request holds the parent's turns through the cut, and nothing after it", async () => {
    const m = await replica()
    const parent = await started(m, 'box')
    await turn(m, parent, 'taller')
    const first = (await m.events.read(parent, 0, 10_000)).map((e) => e.event).find((e) => e.type === 'assistant.text.done')
    if (first?.type !== 'assistant.text.done') throw new Error('no first reply')
    const child = await m.fork(parent, browser, { upTo: first.messageId })
    // The parent goes on past the fork; the child reads none of it.
    await turn(m, parent, 'wider')

    const sent = await turn(await replica(), child.id, 'left')
    expect(sent).toContain('turn:box')
    expect(sent).toContain('reply to box')
    expect(sent).toContain('turn:left')
    expect(sent).not.toContain('turn:taller')
    expect(sent).not.toContain('reply to taller')
    expect(sent).not.toContain('turn:wider')
  }, 120_000)

  it("forgetting the parent leaves the child resumable, and its copied rows open only under the child's key", async () => {
    const m = await replica()
    const parent = await started(m, 'box')
    const child = await m.fork(parent, browser)
    await turn(m, child.id, 'left')
    const before = await m.store.load({ projectKey: '', sessionId: child.id })
    const keys = testPayloadKeys(db.sql)

    await forgetSubject({ sql: db.sql, keys }, `session-${parent}`)
    expect(await ownRows(parent)).toEqual([])
    expect(await link(child.id)).toEqual({ parent: null, cut: null })
    const rows = await ownRows(child.id)
    expect(rows.filter((r) => r.inherited).length).toBeGreaterThan(0)
    // Inherited rows first, the child's own after them, whatever their ids: the same transcript.
    const asChild = new PostgresSessionStore(db.sql, { keys })
    expect(await asChild.load({ projectKey: '', sessionId: child.id })).toEqual(before)
    const sealed = await db.sql<{ sealed: boolean; plain: boolean }[]>`
      SELECT sealed IS NOT NULL AS sealed, entry IS NOT NULL AS plain FROM ai_session_entries WHERE session_id = ${child.id}`
    expect(sealed.every((r) => r.sealed && !r.plain)).toBe(true)
    // Under any other session's key they do not open.
    const other = `session-${randomUUID()}`
    const asOther = new PostgresSessionStore(db.sql, { keys: { keyFor: () => keys.keyFor(other, true) } })
    await expect(asOther.load({ projectKey: '', sessionId: child.id })).rejects.toBeInstanceOf(SealError)

    // A replica that never saw either resumes the child with everything it had.
    const sent = await turn(await replica(), child.id, 'right')
    expect(sent).toContain('turn:box')
    expect(sent).toContain('turn:left')
    expect(sent).toContain('turn:right')
  }, 120_000)

  it('a fork of a fork reads both cuts', async () => {
    const m = await replica()
    const root = await started(m, 'box')
    const middle = await m.fork(root, browser)
    await turn(m, root, 'taller') // past the middle's cut
    await turn(m, middle.id, 'left')
    const leaf = await m.fork(middle.id, browser)
    await turn(m, middle.id, 'right') // past the leaf's cut
    expect(await link(leaf.id)).toMatchObject({ parent: middle.id })
    expect(await ownRows(leaf.id)).toEqual([])

    const sent = await turn(await replica(), leaf.id, 'jump')
    expect(sent).toContain('turn:box')
    expect(sent).toContain('turn:left')
    expect(sent).toContain('turn:jump')
    expect(sent).not.toContain('turn:taller')
    expect(sent).not.toContain('turn:right')

    // The root forgotten: the middle takes its range, and the leaf still reads through the middle.
    await forgetSubject({ sql: db.sql, keys: testPayloadKeys(db.sql) }, `session-${root}`)
    expect(await link(middle.id)).toEqual({ parent: null, cut: null })
    expect(await link(leaf.id)).toMatchObject({ parent: middle.id })
    const again = await turn(await replica(), leaf.id, 'again')
    expect(again).toContain('turn:box')
    expect(again).toContain('turn:left')
    expect(again).toContain('turn:jump')
    expect(again).not.toContain('turn:right')
  }, 180_000)

  it('refuses to forget a parent with forks without the data keys, and forgets nothing', async () => {
    const m = await replica()
    const parent = await started(m, 'box')
    const child = await m.fork(parent, browser)
    await expect(forgetSubject({ sql: db.sql }, `session-${parent}`)).rejects.toThrow(/SCADBUDDY_SECRET_KEY_FILE/)
    expect((await ownRows(parent)).length).toBeGreaterThan(0)
    expect(await link(child.id)).toMatchObject({ parent })
    expect(await db.sql`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${`session-${parent}`}`).toHaveLength(0)
  }, 60_000)

  it('refuses a fork once its parent is being forgotten', async () => {
    const m = await replica()
    const parent = await started(m, 'box')
    // A forget's tombstone is committed before its rows go (forget.ts): a fork that read the
    // parent's transcript before it is refused at its insert, and nothing of it is left.
    await db.sql`INSERT INTO ai_forgotten_subjects (subject) VALUES (${`session-${parent}`})`
    await expect(m.fork(parent, browser)).rejects.toMatchObject({ code: 'not_found' })
    expect(await db.sql`SELECT 1 FROM ai_sessions WHERE parent_id = ${parent}`).toHaveLength(0)
  }, 60_000)
})
