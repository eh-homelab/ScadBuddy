import { randomUUID } from 'node:crypto'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { bundleWorkflowCode, Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { forwardForRun, PluginForwarder, type PluginsForRun } from '../src/plugins/forwarder.js'
import type { PackagesForRun } from '../src/plugins/packages/install.js'
import type { CheckedPlugin } from '../src/plugins/registry.js'
import { respond } from '../src/routes/pendingInput.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { toolCallStarter } from '../src/tools/activityProjection.js'
import { harnessTools } from '../src/tools/harness.js'
import { SessionClients } from '../src/temporal/sessionClients.js'
import { PgApprovalRecords, PgSessionOwners } from '../src/temporal/toolActivities.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { listTurnTools, type ListedTools, turnToolActivity, type TurnToolsDeps } from '../src/temporal/turnTools.js'
import { services } from './helpers/mcp.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions, agentSessionsEnv } from './support/agentSessions.js'

// A turn's plugin and http tools as activities (unified runtime plan 2026-10-13 Step 9.3): with
// `tools_as_activities` on, an AgentSession turn (the real SDK and its bundled Claude Code against
// the fake Anthropic endpoint) is given in-process proxies of its remote plugins in place of the
// plugins themselves, and each call is a ToolCall whose activity `turn:<server>__<tool>` builds the
// server again where it runs. A server that keeps a session (every remote plugin here, as
// Playwright MCP's) is pinned to the turn's own replica's queue, so its state lasts the turn.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const SUBAGENT_MARK = 'TURN-TOOL-SUBAGENT-77aa'

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(`a turn's plugin tools as activities${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
  const tag = randomUUID().slice(0, 8)
  /** The shared queue (agent-tools' place here) and this "replica"'s own (agent-tools.<host>'s). */
  const shared = `turn-calls-${tag}`
  const host = `turn-host-${tag}`
  let workers: Worker[] = []
  let running: Promise<void>[] = []
  let forwarder: PluginForwarder
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply
  let mcp: FakeMcp
  let db: Database
  let drop: () => Promise<void>
  let dir: string
  /** What the workers run with: the running test's. */
  let deps: TurnToolsDeps | undefined

  beforeAll(async () => {
    forwarder = await PluginForwarder.start()
    const env = agentSessionsEnv()
    const activity = (input: unknown) => turnToolActivity(deps!)(input)
    for (const queue of [shared, host]) {
      const worker = await Worker.create({
        connection: env.nativeConnection,
        taskQueue: queue,
        identity: `turn-worker-${queue}`,
        ...(queue === shared ? { workflowBundle: await bundleWorkflowCode({ workflowsPath: WORKFLOWS }) } : {}),
        activities: { default: activity },
      })
      workers.push(worker)
      running.push(worker.run())
    }
  }, 180_000)
  afterAll(async () => {
    for (const w of workers) w.shutdown()
    await Promise.all(running)
    workers = []
    running = []
    await forwarder.close()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    fake = await startFakeAnthropic((r) => script(r))
    mcp = await startFakeMcp({ sessions: true })
    dir = await mkdtemp(path.join(os.tmpdir(), 'turn-tool-call-'))
  })
  afterEach(async () => {
    await stopManagers()
    await fake.close()
    await mcp.close()
    await drop()
    await rm(dir, { recursive: true, force: true })
  })

  async function agent(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const memory: CheckedPlugin = { plugin: { name: 'my-memory', url: mcp.url, toolTiers: { recall: 'read' }, disabledTools: [] }, address: '127.0.0.1' }
    const remotePlugins = (): Promise<PluginsForRun> => Promise.resolve(forwardForRun({ plugins: [memory], problems: [] }, forwarder))
    // A package whose subagent the turn starts.
    const root = path.join(dir, 'greeter')
    for (const [rel, content] of Object.entries({
      '.claude-plugin/plugin.json': JSON.stringify({ name: 'greeter', version: '1.0.0' }),
      'agents/helper.md': `---\nname: helper\ndescription: Helps.\n---\n\nHelp. ${SUBAGENT_MARK}\n`,
    })) {
      await mkdir(path.dirname(path.join(root, rel)), { recursive: true })
      await writeFile(path.join(root, rel), content)
    }
    const packagePlugins = (): Promise<PackagesForRun> =>
      Promise.resolve({ paths: [root], allowedPaths: [], builtinTools: [], loaded: [], problems: [], release: () => {} })
    deps = {
      sessions: new PgSessionOwners(db.sql),
      servers: turnServers({ remotePlugins }),
      hostQueue: host,
      clients: new SessionClients<ListedTools>(),
      approvals: new PgApprovalRecords(db.sql),
      audit: new AuditLog({ sql: db.sql, hashKey: Buffer.alloc(32, 5) }),
    }
    const listDeps = deps
    const start = toolCallStarter({ client: agentSessionsEnv().client, blobs: new SessionBlobs(db.sql), queue: shared })
    return manager({
      sql: db.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-turn-tool-call-token-0123456789ab' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : key === 'tools_as_activities' ? true : undefined) as T) },
      ...harnessTools(services(), []),
      remotePlugins,
      packagePlugins,
      toolCalls: () => start,
      turnTools: (context, only) => listTurnTools(listDeps, context, only),
      approvalPollMs: 50,
      approvalHashKey: Buffer.alloc(32, 5),
    })
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const isSubagent = (r: RecordedRequest) => JSON.stringify(r.body?.system ?? '').includes(SUBAGENT_MARK)
  const toolUses = (r: RecordedRequest) => (r.body?.messages ?? []).flatMap((m) => (Array.isArray(m.content) ? m.content : [])).filter((b) => (b as { type?: string }).type === 'tool_use') as { id: string; name: string }[]

  /** Where each ToolCall of the session ran its activity: its type and queue, from its history. */
  async function activitiesOf(sessionId: string, toolUseId: string): Promise<{ type: string; queue: string }[]> {
    const history = await agentSessionsEnv().client.workflow.getHandle(`session-${sessionId}-call-${toolUseId}`).fetchHistory()
    return (history.events ?? []).flatMap((e) => {
      const a = e.activityTaskScheduledEventAttributes
      return a ? [{ type: a.activityType?.name ?? '', queue: a.taskQueue?.name ?? '' }] : []
    })
  }

  it('runs a remote plugin call as turn:<server>__<tool>, on the turn`s own replica, and keeps its MCP session across two calls', async () => {
    let n = 0
    script = (r) => {
      if (!lastContent(r).includes('tool_result')) return { toolUse: { name: 'mcp__my-memory__recall', input: { query: 'first' } } }
      n += 1
      return n === 1 ? { toolUse: { name: 'mcp__my-memory__recall', input: { query: 'second' } } } : { text: 'Recalled twice.' }
    }
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'recall twice' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(mcp.calls).toEqual(['recall:first', 'recall:second'])
    const ids = [...new Set(fake.messageCalls().flatMap(toolUses).map((t) => t.id))]
    expect(ids).toHaveLength(2)
    for (const id of ids) expect(await activitiesOf(session.id, id)).toEqual([{ type: 'turn:my-memory__recall', queue: host }])
    // One MCP session for both calls: the kept client on this replica.
    expect(mcp.issued).toHaveLength(1)
    expect(fake.messageCalls().map(lastContent).find((c) => c.includes('tool_result'))).toContain('remembered')
  }, 120_000)

  it("a subagent's outward plugin call takes the same path: it parks, and once approved runs on its entry", async () => {
    script = (r) => {
      const last = lastContent(r)
      if (isSubagent(r)) {
        return last.includes('tool_result') ? { text: 'The helper is done.' } : { toolUse: { name: 'mcp__my-memory__retain', input: { text: 'a greeting' } } }
      }
      if (last.includes('tool_result')) return { text: 'All done.' }
      return { toolUse: { name: 'Agent', input: { description: 'Greet', prompt: 'Greet the user.', subagent_type: 'greeter:helper' } } }
    }
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask the helper' })
    let entry: { request_id: string } | undefined
    await expect
      .poll(
        async () => {
          ;[entry] = await db.sql<{ request_id: string }[]>`
            SELECT request_id FROM ai_pending_input WHERE session_id = ${session.id} AND tool = 'mcp__my-memory__retain'`
          return entry?.request_id
        },
        { timeout: 60_000 },
      )
      .toEqual(expect.any(String))
    expect(mcp.calls).toEqual([])
    expect(await respond(m, browser, entry!.request_id, { kind: 'approval', decision: 'approve' })).toMatchObject({ outcome: 'approved' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(mcp.calls).toEqual(['retain:a greeting'])
    const retain = fake.messageCalls().filter(isSubagent).flatMap(toolUses).find((t) => t.name === 'mcp__my-memory__retain')
    expect(await activitiesOf(session.id, retain!.id)).toEqual([{ type: 'turn:my-memory__retain', queue: host }])
  }, 120_000)
})
