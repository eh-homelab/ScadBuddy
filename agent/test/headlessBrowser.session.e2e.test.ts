import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { AGENT_ACTOR_HEADER, AUTHORIZE_TOOL_NAME, SETTING_HEADLESS_BROWSER, TOOL_PREFIX } from '../src/harness/headlessBrowser.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { type PageServer, startOtherOrigin, startUi, testChromium } from './support/browserPages.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, collectUntil, manager, tempPaths } from './support/sessions.js'

// The headless browser in a real session turn (#349): SessionManager, the real
// SDK and Claude Code (scripted model on the fake Anthropic endpoint), the real
// @playwright/mcp and Chromium, approvals and grants in Postgres. The stand-in
// UI's outward route answers with the backend's own GRANT_SQL
// (backend/scadbuddy/api/agent_actor.py) against this database, so the flow is:
//
//   click Print → 403 (no grant) → authorize_request parks for approval →
//   the browser user approves → click Print again → 200 → click again → 403.

const chromium = testChromium()
let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (!chromium ? 'no Chromium' : TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)
const TOKEN = 'gw-headless-session-token-1212343456567878'

const GATE = fileURLToPath(new URL('../../backend/scadbuddy/api/agent_actor.py', import.meta.url))
function grantSql(): { text: string; order: string[] } {
  const raw = /^GRANT_SQL = """([\s\S]*?)"""/m.exec(readFileSync(GATE, 'utf8'))?.[1] ?? ''
  const order: string[] = []
  const text = raw.replace(/%\((\w+)\)s/g, (_m, name: string) => {
    if (!order.includes(name)) order.push(name)
    return `$${order.indexOf(name) + 1}`
  })
  return { text, order }
}

type Block = { type: string; content?: unknown }
function toolResults(r: RecordedRequest): string[] {
  const out: string[] = []
  for (const m of r.body?.messages ?? []) {
    if (!Array.isArray(m.content)) continue
    for (const b of m.content as Block[]) {
      if (b.type === 'tool_result') out.push(typeof b.content === 'string' ? b.content : JSON.stringify(b.content))
    }
  }
  return out
}

describe.skipIf(skip !== undefined)(`the headless browser in a session turn${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let ui: PageServer
  let other: PageServer
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let stop: AbortController
  const pools: Database[] = []
  const statuses: number[] = []

  beforeEach(async () => {
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
    statuses.length = 0
    other = await startOtherOrigin()
    // The backend's gate, as SQL: a marked request passes only with a grant.
    ui = await startUi(other.origin, async (headers) => {
      const marker = headers[AGENT_ACTOR_HEADER.toLowerCase()]
      const { text, order } = grantSql()
      const values: Record<string, string> = { session: String(marker), method: 'POST', path: '/api/v1/prints' }
      const rows = await db.sql.unsafe(text, order.map((n) => values[n]!))
      const status = rows.length === 1 ? 200 : 403
      statuses.push(status)
      return status
    })
  })
  afterEach(async () => {
    stop.abort()
    await fake?.close()
    await ui.close()
    await other.close()
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  async function replica(enabled: boolean, browserAllowedOrigins?: string): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
    pools.push(pool)
    const values: Record<string, unknown> = { model: 'claude-sonnet-4-5', [SETTING_HEADLESS_BROWSER]: enabled }
    return manager({
      sql: pool.sql,
      paths,
      credential: () => Promise.resolve({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve(values[key] as T) },
      headlessBrowser: { backendUrl: ui.origin, ...(browserAllowedOrigins ? { browserAllowedOrigins } : {}), ...chromium },
      approvalPollMs: 50,
    })
  }

  it('refuses the outward click, parks the authorize call, and lets exactly one click through once approved', async () => {
    const click = { name: `${TOOL_PREFIX}browser_click`, input: { element: 'Print', target: '#print' } }
    const wait = (text: string) => ({ name: `${TOOL_PREFIX}browser_wait_for`, input: { text } })
    const steps: Reply[] = [
      { toolUse: { name: `${TOOL_PREFIX}browser_navigate`, input: { url: `${ui.origin}/` } } },
      { toolUse: click },
      { toolUse: wait('print 403') },
      { toolUse: { name: AUTHORIZE_TOOL_NAME, input: { method: 'POST', path: '/api/v1/prints' } } },
      { toolUse: click },
      { toolUse: wait('print 200') },
      { toolUse: click },
      { toolUse: wait('print 403') },
      { text: 'Printed once.' },
    ]
    fake = await startFakeAnthropic((r) => steps[toolResults(r).length] ?? { text: 'Done.' })

    const m = await replica(true)
    const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'print the box in the UI' })
    const events = await m.attach(session.id, agentA, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'approval.required', 60_000)
    const required = seen.at(-1)!.event
    if (required.type !== 'approval.required') throw new Error('unreachable')
    // Before the approval: one refused click, nothing let through.
    expect(statuses).toEqual([403])

    await m.approvals.decide(browser, required.id, true)
    const outcome = await turn!.done
    expect(outcome).toMatchObject({ kind: 'result', subtype: 'success' })

    // Refused, then exactly one request through, then refused again.
    expect(statuses).toEqual([403, 200, 403])
    const results = toolResults(fake.messageCalls().at(-1)!)
    expect(results[3]).toMatch(/Approved: the headless browser may now make POST \/api\/v1\/prints once/)
    const [grant] = await db.sql<{ used: boolean; approval_id: string }[]>`
      SELECT used_at IS NOT NULL AS used, approval_id FROM ai_headless_grants WHERE session_id = ${session.id}`
    expect(grant).toEqual({ used: true, approval_id: required.id })
  }, 120_000)

  it('asks once per origin per session before opening an allowed origin off the backend (SCADBUDDY_BROWSER_ALLOWED_ORIGINS)', async () => {
    const nav = (p: string): Reply => ({ toolUse: { name: `${TOOL_PREFIX}browser_navigate`, input: { url: `${other.origin}${p}` } } })
    fake = await startFakeAnthropic((r) => {
      const n = toolResults(r).length
      const second = JSON.stringify(r.body?.messages ?? []).includes('second look')
      if (!second) return n === 0 ? nav('/one') : n === 1 ? nav('/two') : { text: 'Looked twice.' }
      return n === 2 ? nav('/three') : { text: 'Looked again.' }
    })

    const m = await replica(true, other.origin)
    const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'look at the other site' })
    const events = await m.attach(session.id, agentA, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'approval.required', 60_000)
    const required = seen.at(-1)!.event
    if (required.type !== 'approval.required') throw new Error('unreachable')
    // Parked: nothing reached the other origin before the human decided.
    expect(other.hits).toEqual([])

    await m.approvals.decide(browser, required.id, true)
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    // A later turn of the same session: the origin is remembered, in Postgres.
    expect(await (await m.send(session.id, agentA, 'second look')).done).toMatchObject({ kind: 'result', subtype: 'success' })

    expect(other.hits.map((h) => h.url)).toEqual(['/one', '/two', '/three'])
    for (const hit of other.hits) expect(hit.headers[AGENT_ACTOR_HEADER.toLowerCase()]).toBeUndefined()
    const approvals = await db.sql<{ id: string; tool: string }[]>`
      SELECT id, tool FROM ai_approvals WHERE session_id = ${session.id}`
    expect(approvals).toEqual([{ id: required.id, tool: `${TOOL_PREFIX}browser_navigate` }])
    const origins = await db.sql<{ origin: string; approval_id: string }[]>`
      SELECT origin, approval_id FROM ai_browser_origins WHERE session_id = ${session.id}`
    expect(origins).toEqual([{ origin: other.origin, approval_id: required.id }])
  }, 120_000)

  it('gives a turn no browser while the setting is off', async () => {
    fake = await startFakeAnthropic(() => ({ text: 'No browser.' }))
    const m = await replica(false)
    const { turn } = await m.start(agentA, { origin: 'mcp', prompt: 'look' })
    await turn!.done
    const tools = fake.messageCalls().flatMap((c) => (c.body?.tools ?? []).map((t) => t.name))
    expect(tools.some((t) => t.startsWith(TOOL_PREFIX) || t === AUTHORIZE_TOOL_NAME)).toBe(false)
  }, 60_000)
})
