import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { durableEvalSkip, type DurableEvalStack, startDurableStack } from '../evals/durable.js'
import { DURABLE_EVAL_TOOL_PREFIX } from '../evals/runner.js'
import type { Scenario } from '../evals/runner.js'
import { AGENT_ACTOR_HEADER, PLUGIN_NAME, SERVER_NAME, SETTING_HEADLESS_BROWSER } from '../src/harness/headlessBrowser.js'
import { BrowserHost, turnBrowserServers } from '../src/temporal/turnBrowser.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { SessionFiles } from '../src/workspace/store.js'
import { DOWNLOAD_BODY, type PageServer, startOtherOrigin, startUi, testChromium } from './support/browserPages.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// The headless browser in a durable session against real Chromium (#2526, #2402): the
// agent-durable worker and its bundled Claude Code on the fake Anthropic endpoint, the
// agent's tool workers with the browser's calls pinned to a host queue of their own (as
// main.ts HOST_QUEUE), the real @playwright/mcp and Chromium in BrowserHost, and the
// durable grant in Postgres, checked by the backend's own GRANT_SQL.

const chromium = testChromium()
const skip = durableEvalSkip() ?? (chromium ? undefined : 'no Chromium')
const TOKEN = 'gw-durable-browser-token-1111222233334444'
const HOST_QUEUE = 'agent-tools.e2e-host'
const PREFIX = `${DURABLE_EVAL_TOOL_PREFIX}plugin_${PLUGIN_NAME}_${SERVER_NAME}__`
const AUTHORIZE = `${DURABLE_EVAL_TOOL_PREFIX}scadbuddy_browser__authorize_request`

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

function scenario(id: string, prompt: string): Scenario {
  return { id, title: id, prepare: () => ({ prompt, script: [] }), checks: [] }
}

describe.skipIf(skip !== undefined)(`the headless browser in a durable session${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let ui: PageServer
  let other: PageServer
  let stack: DurableEvalStack
  let host: BrowserHost
  let steps: Reply[] = []
  const statuses: number[] = []

  beforeAll(async () => {
    fake = await startFakeAnthropic((r) => {
      // Only a segment's requests offer tools; anything else (a title, say) gets text.
      if (!r.body?.tools?.some((t) => t.name.startsWith(DURABLE_EVAL_TOOL_PREFIX))) return { text: 'ok' }
      return steps[toolResults(r).length] ?? { text: 'Done.' }
    })
    other = await startOtherOrigin()
    // The backend's gate, as SQL: a marked request passes only with a grant.
    ui = await startUi(other.origin, async (headers) => {
      const { text, order } = grantSql()
      const values: Record<string, string> = { session: String(headers[AGENT_ACTOR_HEADER.toLowerCase()]), method: 'POST', path: '/api/v1/prints' }
      const rows = await stack.sql.unsafe(text, order.map((n) => values[n]!))
      const status = rows.length === 1 ? 200 : 403
      statuses.push(status)
      return status
    })
    stack = await startDurableStack({
      credential: { kind: 'gateway', baseUrl: fake.url, secret: TOKEN },
      settings: { [SETTING_HEADLESS_BROWSER]: true },
      hostQueue: HOST_QUEUE,
      decide: () => 'approve',
      turnServers: ({ sql, stateDir }) => {
        host = new BrowserHost({ paths: { stateDir } })
        return turnServers({
          extra: turnBrowserServers({
            sql,
            paths: { stateDir },
            host,
            headless: { backendUrl: ui.origin, ...chromium },
            files: new SessionFiles(sql),
            settings: { get: async <T,>(key: string) => (key === SETTING_HEADLESS_BROWSER ? true : undefined) as T },
          }),
        })
      },
    })
  }, 300_000)
  afterAll(async () => {
    await host?.stop()
    await stack?.close()
    await fake?.close()
    await ui?.close()
    await other?.close()
  }, 60_000)
  beforeEach(() => {
    statuses.length = 0
  })

  it('refuses the outward click, parks the grant, and lets exactly one click through once approved (#2526)', async () => {
    const click = { name: `${PREFIX}browser_click`, input: { element: 'Print', target: '#print' } }
    const wait = (text: string) => ({ name: `${PREFIX}browser_wait_for`, input: { text } })
    steps = [
      { toolUse: { name: `${PREFIX}browser_navigate`, input: { url: `${ui.origin}/` } } },
      { toolUse: click },
      { toolUse: wait('print 403') },
      { toolUse: { name: AUTHORIZE, input: { method: 'POST', path: '/api/v1/prints' } } },
      { toolUse: click },
      { toolUse: wait('print 200') },
      { toolUse: click },
      { toolUse: wait('print 403') },
      { text: 'Printed once.' },
    ]
    const outcome = await stack.run(scenario('durable-browser-grant', 'print the box in the UI'), { timeoutMs: 180_000 })
    expect(outcome.error).toBeUndefined()
    // The grant was the one call that parked, and was approved.
    expect(outcome.approvals.map((a) => a.toolName)).toEqual([AUTHORIZE])
    // Refused, then exactly one request through, then refused again.
    expect(statuses).toEqual([403, 200, 403])
    const results = toolResults(fake.messageCalls().at(-1)!)
    expect(results[3]).toMatch(/Approved: the headless browser may now make POST \/api\/v1\/prints once/)
    const [grant] = await stack.sql<{ used: boolean }[]>`
      SELECT used_at IS NOT NULL AS used FROM ai_headless_grants WHERE session_id = ${outcome.sessionId!}`
    expect(grant).toEqual({ used: true })
  }, 240_000)

  it("moves a download into the session's workspace after the browser call, and tells the model (#2402)", async () => {
    steps = [
      { toolUse: { name: `${PREFIX}browser_navigate`, input: { url: `${ui.origin}/` } } },
      { toolUse: { name: `${PREFIX}browser_click`, input: { element: 'Download part', target: '#download' } } },
      { text: 'Downloaded.' },
    ]
    const outcome = await stack.run(scenario('durable-browser-download', 'download the part'), { timeoutMs: 180_000 })
    expect(outcome.error).toBeUndefined()
    const files = await stack.sql<{ path: string; data: Buffer; source: unknown }[]>`
      SELECT path, data, source FROM ai_session_files WHERE workspace = ${`session:${outcome.sessionId!}`} ORDER BY path`
    expect(files.map((f) => [f.path, f.data.toString('utf8'), f.source])).toEqual([
      ['downloads/part.stl', DOWNLOAD_BODY, { tool: 'browser', url: `${ui.origin}/files/part.stl` }],
    ])
    expect(toolResults(fake.messageCalls().at(-1)!)[1]).toMatch(/Headless-browser downloads[^"]*downloads\/part\.stl/)
  }, 240_000)
})
