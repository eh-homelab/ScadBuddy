import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { Client, WorkflowNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { tiersUpTo, type Principal } from '../src/auth/principal.js'
import { TabHub, type TabConnection } from '../src/bridge/hub.js'
import { PostgresPairingStore } from '../src/bridge/pairings.js'
import type { AgentFrame } from '../src/bridge/protocol.js'
import { TemporalTabRelay } from '../src/bridge/relay.js'
import { PostgresSessionTabStore } from '../src/bridge/sessionTabs.js'
import { PostgresTabOwnerStore } from '../src/bridge/tabOwners.js'
import { connectDatabase, type Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { runHarness } from '../src/harness/run.js'
import { callContext } from '../src/harness/callContext.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { queuesPolled } from '../src/runtime/health.js'
import { roleParts } from '../src/runtime/role.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { resolveTabWaits, type SessionManager } from '../src/sessions/manager.js'
import { RUNTIME_DEFAULTS } from '../src/sessions/runtimeSettings.js'
import { wireRuntimeSync } from '../src/sessions/runtimeSync.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { dropPayloadKey, FilePayloadStore, type PgPayloadKeys, subjectDataConverter } from '../src/temporal/payloadCodec.js'
import { acquireActivities, liveHostQueues, releaseActivities } from '../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { sessionGateActivities } from '../src/temporal/session/gateActivities.js'
import { type HostWorkers, startHostWorkers } from '../src/temporal/session/hostWorkers.js'
import { SESSION_QUEUE, SESSION_SETTINGS_ACTIVITY, sessionHostQueue } from '../src/temporal/session/names.js'
import { sessionTurnActivity } from '../src/temporal/session/turnActivity.js'
import { AgentWorker, workerIdentity } from '../src/temporal/worker.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// SCADBUDDY_AGENT_ROLE end to end (spec 2026-10-13 §14.3 and §14.5, plan Step 6.1): one `api`
// process and one `worker` process on one Temporal dev server and one Postgres, each wired as
// main.ts wires its role (runtime/role.ts `roleParts` decides which workers each starts), with
// the real Claude Code against the fake Anthropic endpoint and every payload sealed, as main.ts's
// clients and workers seal them with the KEK. The API's process starts the session and holds the
// tab; it polls no `agent-sessions`, no shared `agent-tools` and no session host queue, so every
// turn runs on the worker. What crosses between them is what main.ts gives the turn on a worker:
// Temporal (the session, BridgeCall), Postgres rows and NOTIFY (interrupt, eviction).

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = !TEMPORAL_CLI
  ? TEMPORAL_SKIP
  : cliMissing
    ? ` (skipped: ${cliMissing})`
    : TEST_DATABASE_URL
      ? ''
      : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const TAB = 'tab-aaaaaaaaaaaaaaaaaaaaaa'
const PROBE = 'mcp__stub__tab_probe'
const owner: Principal = { id: browser.id, kind: 'browser', tiers: tiersUpTo('outward') }
const conversation = (r: RecordedRequest) => JSON.stringify(r.body?.messages ?? [])

/** One agent process of a role, as main.ts builds it. */
type AgentProcess = {
  role: 'api' | 'worker'
  identity: string
  /** `agent-tools.<identity>`: its own host queue, which every role polls. */
  hostQueue: string
  pool: Database
  m: SessionManager
  tabs: TabHub
  listener: PgEventListener
  hosts: HostWorkers
  shared: AgentWorker[]
  turnHost: TurnHost | undefined
  /** Claude Code processes it spawned. */
  spawned: number
  stopRuntimeSync: () => void
}

describe.skipIf(skip !== '')(`one api process and one worker process${skip}`, { timeout: 180_000 }, () => {
  let env: TestWorkflowEnvironment
  let bundle: Awaited<ReturnType<typeof bundleWorkflowCode>>
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply | Promise<Reply>
  let db: Database
  let schema: string
  let url: string
  let drop: () => Promise<void>
  let payloadDir: string
  let keys: PgPayloadKeys
  let dataConverter: ReturnType<typeof subjectDataConverter>
  let client: Client
  let tag: string
  let processes: AgentProcess[]
  /** The tab's calls, each frame as the tab got it. */
  let tabCalls: Extract<AgentFrame, { type: 'call' }>[]

  beforeAll(async () => {
    env = await localTemporal()
    bundle = await bundleWorkflowCode({ workflowsPath: WORKFLOWS })
  }, 180_000)
  afterAll(async () => {
    await env?.teardown()
  })

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    payloadDir = await mkdtemp(join(tmpdir(), 'agent-role-payloads-'))
    keys = testPayloadKeys(db.sql)
    dataConverter = subjectDataConverter(keys, new FilePayloadStore(payloadDir))
    client = new Client({ connection: env.connection, namespace: env.client.options.namespace, dataConverter })
    tag = randomUUID().slice(0, 8)
    processes = []
    tabCalls = []
    script = () => ({ text: 'ok' })
  }, 90_000)
  afterEach(async () => {
    for (const { id } of await db.sql<{ id: string }[]>`SELECT id FROM ai_sessions`) {
      await client.workflow
        .getHandle(sessionWorkflowId(id))
        .terminate('the test ended')
        .catch((err: unknown) => {
          if (!(err instanceof WorkflowNotFoundError)) throw err
        })
    }
    for (const p of processes.splice(0)) {
      const hosted = p.turnHost?.stop({ graceMs: 1_000, abortWaitMs: 1_000 })
      await Promise.all([...p.shared.map((w) => w.stop()), p.hosts.stop(), p.m.stopTurns({ graceMs: 1_000, abortWaitMs: 1_000, ...(hosted ? { others: hosted } : {}) })])
      p.stopRuntimeSync()
      p.tabs.close()
      await p.listener.close()
      await p.pool.close()
    }
    await fake.close()
    await drop()
    await rm(payloadDir, { recursive: true, force: true })
  }, 60_000)

  const pollUntil = <T>(read: () => Promise<T> | T, expected: T, timeout = 60_000) => expect.poll(read, { timeout, interval: 100 }).toEqual(expected)

  /** The identities polling `queue` (1 workflow, 2 activity) now. */
  async function pollers(queue: string, type: 1 | 2): Promise<string[]> {
    const { pollers } = await env.client.workflowService.describeTaskQueue({ namespace: env.client.options.namespace, taskQueue: { name: queue }, taskQueueType: type })
    return (pollers ?? []).flatMap((p) => (p.identity ? [p.identity] : []))
  }

  async function agentProcess(role: 'api' | 'worker'): Promise<AgentProcess> {
    const parts = roleParts(role)
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(url, { searchPath: schema })
    const identity = `${role}-${tag}`
    const hostQueue = `${TASK_QUEUE}.${identity}`
    const sessionHost = sessionHostQueue(identity)
    const p = { role, identity, hostQueue, pool, spawned: 0, shared: [] as AgentWorker[] } as AgentProcess
    const listener = new PgEventListener(url, { searchPath: schema, retryMinMs: 20, log: () => {} })
    p.listener = listener
    // The browser relay, sealed (main.ts `tabRelay.useClient`): a call for a tab on another process is a BridgeCall.
    const relay = new TemporalTabRelay({
      owners: new PostgresTabOwnerStore(pool.sql),
      hostQueue,
      client: { client, dropKey: (subject: string) => dropPayloadKey(pool.sql, keys, subject) },
      log: () => {},
    })
    p.tabs = new TabHub({ pairings: new PostgresPairingStore(pool.sql), relay, sessionTabs: new PostgresSessionTabStore(pool.sql), log: () => {} })
    p.m = manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-agent-role-test-token' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      // A browser call as the browser_* tools make it (tools/browser.ts): through this process's hub, for the turn's
      // session; one that finds no tab waits for it on the turn's gate, and is made once more when it is back.
      mcpServers: (session, _principal, extras) => ({
        stub: createSdkMcpServer({
          name: 'stub',
          tools: [
            tool('tab_probe', 'Reads the page in the ScadBuddy tab', {}, async (_args, extra) => {
              const target = { principal: owner, sessionId: session.id! }
              const call = () => p.tabs.call(target, 'snapshot', {}, { signal: AbortSignal.timeout(20_000) })
              let outcome = await call()
              const context = callContext(extra)
              if (!outcome.ok && outcome.error.code === 'no_browser' && extras?.waitForTab && context.ok) {
                const waited = await extras.waitForTab({
                  tool: 'browser_snapshot',
                  toolUseId: context.toolUseId,
                  signal: context.signal,
                  isBack: async (signal) => (await p.tabs.status(target, { signal })).attached,
                })
                if (!waited.back) return { content: [{ type: 'text', text: `no tab: ${waited.message}` }], isError: true }
                outcome = await call()
              }
              return { content: [{ type: 'text', text: JSON.stringify(outcome) }] }
            }),
          ],
        }),
      }),
      tierOf: (n) => (n === PROBE ? 'read' : undefined),
      warm: { maxEntries: 8 },
      run: (run) =>
        runHarness({
          ...run,
          spawnClaudeCodeProcess: (o) => {
            p.spawned++
            return spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'ignore'] })
          },
        }),
    })
    // Readiness reads the live session hosts as main.ts does (`liveHostQueues`), cut to this test's.
    p.m.durableTurns = new DurableTurns({
      client,
      sql: pool.sql,
      events: p.m.events,
      liveHosts: async () => new Set([...(await liveHostQueues(client))].filter((q) => processes.some((other) => sessionHostQueue(other.identity) === q))),
    })
    p.m.durable = new DurableGate(client, { timeoutMs: 30_000, db: { sql: pool.sql, events: p.m.events } })
    // As main.ts: the hub's process resolves a session's tab waits; every process follows the evictions.
    if (parts.http) resolveTabWaits(p.tabs, p.m)
    p.stopRuntimeSync = wireRuntimeSync({ settings: { get: () => Promise.resolve(undefined) }, warm: p.m.warm, revalidate: () => p.m.revalidateWarm(), events: listener, log: () => {} })
    listener.start()

    p.turnHost = parts.sessionHost
      ? new TurnHost({
          core: p.m.turnCore,
          sql: pool.sql,
          images: new SessionBlobs(pool.sql),
          hostQueue: sessionHost,
          listen: (channel, onNotify) => listener.listenAlso(channel, onNotify),
        })
      : undefined
    const acquire = parts.sessionHost
      ? acquireActivities({ warm: p.m.warm, hostQueue: sessionHost, identity, liveHosts: async () => new Set([sessionHost]) })
      : {}
    const common = { address: env.address, namespace: env.client.options.namespace, identity: workerIdentity(identity), dataConverter, shutdownGraceMs: 1_000, log: () => {} }
    if (parts.sharedWorkers) {
      p.shared.push(
        AgentWorker.start({
          ...common,
          taskQueue: TASK_QUEUE,
          activities: { ...acquire, ...sessionFinishActivities(pool.sql, p.m.events), ...sessionGateActivities({ sql: pool.sql, events: p.m.events }) },
          // BridgeCall and AgentSession's own bundle, as every shared agent-tools worker carries it.
          workflows: { workflowBundle: bundle },
        }),
        AgentWorker.start({
          ...common,
          taskQueue: SESSION_QUEUE,
          activities: { [SESSION_SETTINGS_ACTIVITY]: async () => RUNTIME_DEFAULTS },
          workflows: { workflowBundle: bundle },
        }),
      )
    }
    p.hosts = startHostWorkers({
      address: env.address,
      namespace: env.client.options.namespace,
      identity,
      dataConverter,
      pinned: { ...relay.hostActivities() },
      ...(p.turnHost
        ? {
            sessions: {
              activities: { ...sessionTurnActivity(p.turnHost, { client }), ...releaseActivities(p.m.warm), ...acquire },
              slots: 10,
              shutdownGraceMs: 2_000,
              shutdownForceMs: 3_000,
            },
          }
        : {}),
      shutdownGraceMs: 1_000,
    })
    await Promise.all([...p.shared.map((w) => w.running()), p.hosts.pinned.running(), p.hosts.sessions?.running()])
    relay.startHosting()
    processes.push(p)
    if (role === 'worker') {
      // Its /readyz (runtime/health.ts): Temporal lists it on every queue it serves.
      const queues = [
        { name: SESSION_QUEUE, type: 1 as const },
        { name: TASK_QUEUE, type: 2 as const },
        { name: hostQueue, type: 2 as const },
        { name: sessionHost, type: 2 as const },
      ]
      await pollUntil(() => queuesPolled(client, queues, workerIdentity(identity)), undefined)
    } else {
      await pollUntil(async () => (await pollers(hostQueue, 2)).includes(workerIdentity(identity)), true)
    }
    return p
  }

  /** A tab connected to `p`'s hub, answering every call with where it is; claimed for `p`'s host queue. */
  async function tabOn(p: AgentProcess): Promise<TabConnection> {
    const conn: TabConnection = p.tabs.open((frame) => {
      if (frame.type !== 'call') return
      tabCalls.push(frame)
      queueMicrotask(() => void conn.receive(JSON.stringify({ v: 1, type: 'result', id: frame.id, outcome: { ok: true, result: `the page, from the ${p.role} process` } })))
    })
    await conn.receive(JSON.stringify({ v: 1, type: 'hello', tabId: TAB, route: '/m/box', live: ['snapshot'] }))
    await vi.waitFor(async () => {
      const [row] = await db.sql<{ queue: string }[]>`SELECT queue FROM ai_tab_owners WHERE tab_id = ${TAB}`
      expect(row?.queue).toBe(p.hostQueue)
    })
    return conn
  }

  async function startOn(p: AgentProcess, prompt: string) {
    const started = await p.m.start(browser, { origin: 'chat', prompt })
    return { id: started.session.id, turn: started.turn! }
  }
  const settled = (id: string) => pollUntil(() => client.workflow.getHandle(sessionWorkflowId(id)).query<string | null>('turn'), null)

  it('a unified session sent through api runs its turn on worker; api polls none of the turn queues', async () => {
    const api = await agentProcess('api')
    const worker = await agentProcess('worker')
    const { turn } = await startOn(api, 'hello')
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(worker.spawned).toBe(1)
    expect(worker.m.warm.size()).toBe(1)
    expect(api.spawned).toBe(0)
    expect(api.m.warm.size()).toBe(0)
    const apiPoller = workerIdentity(api.identity)
    expect(await pollers(SESSION_QUEUE, 1)).not.toContain(apiPoller)
    expect(await pollers(TASK_QUEUE, 2)).not.toContain(apiPoller)
    expect(await pollers(TASK_QUEUE, 1)).not.toContain(apiPoller)
    expect(await pollers(sessionHostQueue(api.identity), 2)).toEqual([])
    expect(await pollers(sessionHostQueue(worker.identity), 2)).toContain(workerIdentity(worker.identity))
  })

  it("a browser call from the worker's turn reaches the tab connected to api", async () => {
    const api = await agentProcess('api')
    await agentProcess('worker')
    script = (r) =>
      conversation(r).includes('look at the page') && !conversation(r).includes('tool_result') ? { toolUse: { name: PROBE, input: {} } } : { text: 'seen' }
    const { id, turn } = await startOn(api, 'hello')
    await turn.done
    await settled(id)
    await tabOn(api)
    // The panel's message from that tab pairs it with the session (routes/chat.ts).
    api.tabs.pairSession(id, TAB)
    const second = await api.m.send(id, browser, 'look at the page')
    expect(await second.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(tabCalls).toHaveLength(1)
    expect(tabCalls[0]).toMatchObject({ tool: 'snapshot' })
    expect(conversation(fake.messageCalls().at(-1)!)).toContain('the page, from the api process')
  })

  it('an interrupt from api ends the turn on worker within 2 s, and keeps its process', async () => {
    const api = await agentProcess('api')
    const worker = await agentProcess('worker')
    let answer: () => void = () => {}
    script = () =>
      new Promise<Reply>((resolve) => {
        answer = () => resolve({ text: 'too late' })
      })
    const { id, turn } = await startOn(api, 'think hard')
    await pollUntil(() => fake.messageCalls().length, 1)
    const asked = performance.now()
    expect(await api.m.interrupt(id, browser)).toBe(true)
    expect(await turn.done).toMatchObject({ kind: 'interrupted' })
    expect(performance.now() - asked).toBeLessThan(2_000)
    answer()
    expect(worker.m.warm.size()).toBe(1)
  })

  it("a forget from api closes the worker's warm process", async () => {
    const api = await agentProcess('api')
    const worker = await agentProcess('worker')
    const { id, turn } = await startOn(api, 'hello')
    await turn.done
    await settled(id)
    expect(worker.m.warm.size()).toBe(1)
    await forgetSubject({ sql: api.pool.sql, client, keys }, `session-${id}`)
    await pollUntil(() => worker.m.warm.size(), 0, 10_000)
  })

  it("a tab that drops and comes back on api resolves the worker's turn's tab_disconnected park", async () => {
    const api = await agentProcess('api')
    const worker = await agentProcess('worker')
    script = (r) => (conversation(r).includes('tool_result') ? { text: 'seen' } : { toolUse: { name: PROBE, input: {} } })
    // No tab is connected: the worker's turn finds none, and waits for it through its workflow (#5003).
    const { id, turn } = await startOn(api, 'look at the page')
    await pollUntil(async () => (await db.sql<{ reason: string }[]>`SELECT attention->>'reason' AS reason FROM ai_pending_input WHERE session_id = ${id}`).map((r) => r.reason), ['tab_disconnected'])
    expect(tabCalls).toEqual([])
    const sent = vi.spyOn(api.m.durable!, 'tabReconnected')
    // The tab connects to api, and the panel pairs it with the session: api's hub sees the session's tab again.
    await tabOn(api)
    api.tabs.pairSession(id, TAB)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    const [response] = await db.sql<{ outcome: string; reason: string }[]>`SELECT outcome, reason FROM ai_input_responses WHERE session_id = ${id}`
    expect(response).toEqual({ outcome: 'cancelled', reason: 'the ScadBuddy tab is connected again' })
    // api's hub ended it (resolveTabWaits): its `tab_reconnected` ended the one wait.
    expect(sent).toHaveBeenCalledWith(id)
    expect(await sent.mock.results[0]!.value).toBe(1)
    // The call made once more on the worker reached the tab on api, and the turn held its process throughout.
    expect(tabCalls).toHaveLength(1)
    expect(conversation(fake.messageCalls().at(-1)!)).toContain('the page, from the api process')
    expect(worker.spawned).toBe(1)
    expect(api.spawned).toBe(0)
  })
})
