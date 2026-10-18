import { Client } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { CredentialStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { kekFromBase64 } from '../src/secrets.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type SessionManager, SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import { PgFlowRuns } from '../src/temporal/flowRuns.js'
import { flowSessionActivities } from '../src/temporal/flowSessions.js'
import { PgPayloadKeys, SubjectPayloadCodec } from '../src/temporal/payloadCodec.js'
import { gateActivities, PgApprovalRecords, PgSessionOwners, toolActivities } from '../src/temporal/toolActivities.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { turnToolActivities } from '../src/temporal/turnTools.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { DURABLE_TOOLS, writeManifest } from '../src/tools/manifest.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { durableHealthPort } from './support/durableHealth.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// A flow's `agent(...)` and `ask_session(...)` end to end (#1057, plan
// 2026-10-09-durable-phase-6-flows.md §8: "agent(...) and ask_session against the fake
// endpoint"). Every piece is the real one, on one Temporal dev server and one schema:
//
// - the backend's `projects` worker runs the script in ProjectWorkflow
//   (backend/tests/flows/agent_e2e_driver.py, a process);
// - this process's agent-tools worker serves `sessions_start`/`sessions_send` (the
//   SessionManager, durable, over DurableTurns), `flow_session_check`/`flow_session_turn`
//   and the session's own tool calls, as main.ts wires it;
// - agent-durable's worker (`python -m scadbuddy_durable`, a process) runs the
//   DurableSession turn with the bundled Claude Code, pointed as a gateway at this
//   process's fake Anthropic endpoint (support/fakeAnthropic.ts), which scripts the turn.
//
// backend/tests/flows/test_agent_host_functions.py covers the host functions against
// stand-in activities, and flowSessions.pg.test.ts the two activities against a
// hand-written event log; this is the path between them. Skips without uv, Temporal or
// Postgres.

const BACKEND_DIR = fileURLToPath(new URL('../../backend/', import.meta.url))
const DURABLE_DIR = fileURLToPath(new URL('../../agent-durable/', import.meta.url))
const FAILURE_CONVERTER = fileURLToPath(new URL('../src/temporal/failureConverter.ts', import.meta.url))
const TOOLS_QUEUE = 'agent-tools'
const TOKEN = 'gateway-token-1057'
// The durable plugin offers each tool to Claude Code as `mcp__durable__<name>`.
const DURABLE = 'mcp__durable__'
const SIZE_PROMPT = 'Size the door'
const WINDOW_PROMPT = 'And the window?'
const SCHEMA = { type: 'object', properties: { width: { type: 'number' } }, required: ['width'] }

function hasUv(): boolean {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const skip = !hasUv()
  ? 'uv is not on PATH'
  : !TEMPORAL_CLI
    ? TEMPORAL_SKIP.trim()
    : !TEST_DATABASE_URL
      ? `${TEST_DATABASE_URL_ENV} is not set`
      : undefined

function withSearchPath(url: string, schema: string): string {
  const u = new URL(url)
  u.searchParams.set('options', `-csearch_path=${schema}`)
  return u.toString()
}

/** How many tool results the model has been handed in this request's conversation. */
function toolResults(request: RecordedRequest): number {
  return (request.body?.messages ?? [])
    .flatMap((m) => (Array.isArray(m.content) ? (m.content as { type?: string }[]) : []))
    .filter((block) => block.type === 'tool_result').length
}

/**
 * The scripted model. The `agent(...)` turn reads its sessions (a read call), tries to
 * delete an output (an outward call, which the test denies), then answers in the JSON the
 * schema asks for, fenced. The `ask_session` turn answers in words. Claude Code's own side
 * requests (no tools offered) get "ok".
 */
function reply(request: RecordedRequest): Reply {
  const tools = (request.body?.tools ?? []).map((t) => t.name)
  if (!tools.includes(`${DURABLE}delete_output`)) return { text: 'ok' }
  const conversation = JSON.stringify(request.body?.messages ?? [])
  if (conversation.includes(WINDOW_PROMPT)) return { text: 'The window is 60 wide.' }
  if (!conversation.includes(SIZE_PROMPT)) return { text: 'ok' }
  const results = toolResults(request)
  if (results === 0) return { toolUse: { name: `${DURABLE}sessions_list`, input: {} } }
  if (results === 1) return { toolUse: { name: `${DURABLE}delete_output`, input: { output_id: 'out-1' } } }
  return { text: '```json\n{"width": 40}\n```' }
}

type FlowStep = { fn: string; call_id: string; status: string; outward: boolean; session_id: string | null }
type FlowAnswer = { run_id: string; status: string; result: string | null; steps: FlowStep[]; preview: [string, string][] }

/** A child process, its output kept for a failure's message. */
type Proc = { child: ChildProcess; log: () => string }

function start(cmd: string, args: string[], cwd: string, env: Record<string, string>, stdin: 'pipe' | 'ignore'): Proc {
  let log = ''
  const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, stdio: [stdin, 'pipe', 'pipe'] })
  child.stderr?.on('data', (d: Buffer) => (log += d.toString()))
  if (stdin === 'ignore') child.stdout?.on('data', (d: Buffer) => (log += d.toString()))
  return { child, log: () => log }
}

async function stop(proc: Proc | undefined): Promise<void> {
  const child = proc?.child
  if (!child || child.exitCode !== null) return
  const exited = new Promise((resolve) => child.once('exit', resolve))
  child.kill('SIGTERM')
  await Promise.race([exited, new Promise((r) => setTimeout(r, 15_000))])
  if (child.exitCode === null) child.kill('SIGKILL')
}

describe.skipIf(skip !== undefined)(`a flow's agent() and ask_session() end to end${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let fake: FakeAnthropic
  let durable: Proc | undefined
  let projects: Proc | undefined
  let worker: AgentWorker | undefined
  let client: Client
  let sessions: SessionManager
  let gate: DurableGate
  const lines: string[] = []
  const waiting: ((line: string) => void)[] = []

  /** Runs `script` as a flow run in the backend's `projects` worker. */
  async function flow(...body: string[]): Promise<FlowAnswer> {
    const script = ['import asyncio', 'async def main():', ...body.map((b) => `    ${b}`), 'asyncio.run(main())'].join('\n')
    const answer = new Promise<string>((resolve) => waiting.push(resolve))
    projects!.child.stdin!.write(`${JSON.stringify({ script })}\n`)
    let onExit: (() => void) | undefined
    const exited = new Promise<never>((_, reject) => {
      onExit = () => reject(new Error(`the projects worker exited: ${projects!.log()}`))
      projects!.child.once('exit', onExit)
    })
    try {
      return JSON.parse(await Promise.race([answer, exited])) as FlowAnswer
    } finally {
      projects!.child.off('exit', onExit!)
    }
  }

  beforeAll(async () => {
    env = await localTemporal()
    let schema: string
    let url: string
    ;({ db, url, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const dir = await mkdtemp(path.join(os.tmpdir(), 'flow-sessions-e2e-'))
    const kekB64 = randomBytes(32).toString('base64')
    const kekFile = path.join(dir, 'kek')
    await writeFile(kekFile, kekB64, { mode: 0o600 })
    await writeManifest(path.join(dir, 'tools.json'))
    for (const sub of ['home', 'cwd']) await mkdir(path.join(dir, sub))
    const kek = kekFromBase64(kekB64)
    fake = await startFakeAnthropic(reply)
    await new CredentialStore(db.sql).create({ kind: 'gateway', base_url: fake.url, secret: TOKEN }, kek)
    const databaseUrl = withSearchPath(url, schema)

    // agent-durable, with the real segment runner (no SCADBUDDY_DURABLE_SCRIPTED).
    durable = start(
      'uv',
      ['run', '--frozen', 'python', '-m', 'scadbuddy_durable'],
      DURABLE_DIR,
      {
        SCADBUDDY_DATABASE_URL: databaseUrl,
        SCADBUDDY_TEMPORAL_ADDRESS: env.address,
        SCADBUDDY_TEMPORAL_NAMESPACE: 'default',
        SCADBUDDY_SECRET_KEY_FILE: kekFile,
        SCADBUDDY_DURABLE_TOOLS_JSON: path.join(dir, 'tools.json'),
        SCADBUDDY_DURABLE_CWD: path.join(dir, 'cwd'),
        SCADBUDDY_DURABLE_HEALTH_PORT: '0',
        // Claude Code's own state, never the developer's.
        HOME: path.join(dir, 'home'),
        CLAUDE_CONFIG_DIR: path.join(dir, 'home'),
      },
      'ignore',
    )
    // The backend's `projects` worker; it migrates the backend's tables into the schema.
    projects = start(
      'uv',
      ['run', '--frozen', 'python', '-m', 'tests.flows.agent_e2e_driver'],
      BACKEND_DIR,
      {
        SCADBUDDY_DATABASE_URL: databaseUrl,
        SCADBUDDY_TEMPORAL_ADDRESS: env.address,
        SCADBUDDY_SECRET_KEY_FILE: kekFile,
        SCADBUDDY_FLOW_TOOLS_QUEUE: TOOLS_QUEUE,
      },
      'pipe',
    )
    createInterface({ input: projects.child.stdout! }).on('line', (line) => {
      const next = waiting.shift()
      if (next) next(line)
      else lines.push(line)
    })

    // This process: the sessions, and the agent-tools worker as main.ts wires it.
    const codec = { payloadCodecs: [new SubjectPayloadCodec(new PgPayloadKeys(db.sql, kek))] }
    client = new Client({ connection: env.connection, namespace: 'default', dataConverter: codec })
    sessions = manager({
      sql: db.sql,
      paths: await tempPaths(),
      settings: { get: <T>(key: string) => Promise.resolve((key === SETTING_SESSION_MODE ? 'durable' : undefined) as T) },
    })
    gate = new DurableGate(client)
    sessions.durable = gate
    sessions.durableTurns = new DurableTurns({ client, sql: db.sql, events: sessions.events, sendTimeoutMs: 60_000 })
    const services = {
      backend: createBackendClient('http://backend.invalid', async () => new Response(null, { status: 503 })),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: 5_000,
      sessions,
    }
    const audit = {
      record: async () => {},
      hash: (tool: string, input: unknown) => createHash('sha256').update(JSON.stringify([tool, input])).digest('hex'),
      summarise: (tool: string) => `call ${tool}`,
    }
    const flows = new PgFlowRuns(db.sql)
    worker = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue: TOOLS_QUEUE,
      activities: {
        ...toolActivities(DURABLE_TOOLS, {
          services,
          sessions: new PgSessionOwners(db.sql),
          approvals: new PgApprovalRecords(db.sql),
          flows,
          audit,
        }),
        ...gateActivities({ audit, sessions: new PgSessionOwners(db.sql) }),
        // A durable turn lists its other tools first (#2320): none here.
        ...turnToolActivities({ sessions: new PgSessionOwners(db.sql), servers: turnServers({}) }),
        ...flowSessionActivities({ sql: db.sql, flows, pollMs: 200 }),
      },
      dataConverter: { ...codec, failureConverterPath: FAILURE_CONVERTER },
    })
    await worker.running()

    // Both workers up (uv may sync their environments first).
    const deadline = Date.now() + 240_000
    let port: number | undefined
    for (;;) {
      if (durable.child.exitCode !== null) throw new Error(`the durable worker exited: ${durable.log()}`)
      if (projects.child.exitCode !== null) throw new Error(`the projects worker exited: ${projects.log()}`)
      port ??= durableHealthPort(durable.log())
      const status = port === undefined
        ? undefined
        : await fetch(`http://127.0.0.1:${port}/healthz`)
            .then((r) => r.json() as Promise<{ worker?: string }>)
            .catch(() => undefined)
      if (status?.worker === 'running' && lines.includes('{"ready": true}')) break
      if (Date.now() > deadline) throw new Error(`the workers did not start: ${durable.log()}\n${projects.log()}`)
      await new Promise((r) => setTimeout(r, 500))
    }
  }, 300_000)

  afterAll(async () => {
    projects?.child.stdin?.end()
    await stop(projects)
    const ids = db ? await db.sql<{ id: string }[]>`SELECT id::text FROM ai_sessions WHERE mode = 'durable'`.catch(() => []) : []
    for (const { id } of ids) {
      await client?.workflow
        .getHandle(sessionWorkflowId(id))
        .terminate('test over')
        .catch(() => {})
    }
    await worker?.stop()
    await stop(durable)
    await fake?.close()
    await drop?.()
    await env?.teardown()
  }, 60_000)

  /** Denies the first outward call that parks at a gate, unless `done` settles first. */
  async function denyTheOutwardCall(done: Promise<unknown>): Promise<string | undefined> {
    let settled = false
    void done.finally(() => (settled = true)).catch(() => {})
    const deadline = Date.now() + 180_000
    for (;;) {
      if (settled) return undefined
      const [parked] = await db.sql<{ request_id: string; session_id: string; tool: string }[]>`
        SELECT p.request_id, p.session_id::text, p.tool FROM ai_pending_input p
         WHERE p.kind = 'approval' AND NOT EXISTS (SELECT 1 FROM ai_input_responses r WHERE r.request_id = p.request_id)`
      if (parked) {
        const answer = await gate.respond(parked.session_id, {
          request_id: parked.request_id,
          response: { kind: 'approval', decision: 'deny' },
          responder: browser,
          role: 'browser',
        })
        expect(answer).toEqual({ kind: 'approval', outcome: 'denied' })
        return parked.tool
      }
      if (Date.now() > deadline) throw new Error(`no outward call parked: ${durable!.log()}`)
      await new Promise((r) => setTimeout(r, 200))
    }
  }

  it("agent() starts a durable session, waits for its turn and parses the answer its result_schema asks for", async () => {
    const running = flow(`answer = await agent(${JSON.stringify(SIZE_PROMPT)}, [], ${JSON.stringify(SCHEMA)})`, "return answer['width'] + 2")
    // A run that ends without parking a call fails below with its own result.
    const [run, denied] = await Promise.all([running, denyTheOutwardCall(running)])
    expect(denied).toBe('delete_output')
    // The fenced JSON was parsed and checked against the schema: the script did arithmetic on it.
    expect([run.status, run.result]).toEqual(['succeeded', 'result: 42'])
    const [step] = run.steps
    expect(run.steps).toHaveLength(1)
    expect(step).toMatchObject({ fn: 'agent', status: 'succeeded', outward: false })
    const sessionId = step!.session_id!
    const [row] = await db.sql<{ mode: string; owner_id: string }[]>`SELECT mode, owner_id FROM ai_sessions WHERE id = ${sessionId}`
    expect(row).toEqual({ mode: 'durable', owner_id: 'browser' })

    // The model was asked for that JSON, in the session the flow started.
    const asked = fake.messageCalls().find((r) => JSON.stringify(r.body?.messages ?? []).includes(SIZE_PROMPT))
    expect(JSON.stringify(asked?.body?.messages)).toContain('answer with only a JSON value that matches this JSON Schema')

    // The turn's one outward call (the denied delete_output, not the sessions_list read)
    // was counted: a Reset to before the step would make it again.
    const calls = await db.sql<{ event: { type: string; name: string; risk: string } }[]>`
      SELECT event::jsonb AS event FROM ai_session_events
       WHERE session_id = ${sessionId} AND event::jsonb->>'type' = 'tool.call' ORDER BY seq`
    expect(calls.map((c) => [c.event.name, c.event.risk])).toEqual([
      ['sessions_list', 'read'],
      ['delete_output', 'outward'],
    ])
    expect(run.preview).toEqual([['agent', step!.call_id]])
  }, 300_000)

  it('ask_session() sends to a durable session the starter owns and answers with its turn', async () => {
    const { session } = await sessions.start(browser, { origin: 'chat', title: 'window', mode: 'durable' })
    expect(session.mode).toBe('durable')
    const run = await flow(`return await ask_session(${JSON.stringify(session.id)}, ${JSON.stringify(WINDOW_PROMPT)})`)
    expect([run.status, run.result]).toEqual(['succeeded', "result: 'The window is 60 wide.'"])
    expect(run.steps.map((s) => [s.fn, s.status, s.session_id])).toEqual([['ask_session', 'succeeded', session.id]])
    // A turn with no outward call is not one a Reset would repeat.
    expect(run.preview).toEqual([])
    const [turn] = await db.sql<{ text: string }[]>`
      SELECT event::jsonb->>'text' AS text FROM ai_session_events
       WHERE session_id = ${session.id} AND event::jsonb->>'type' = 'user.turn'`
    expect(turn?.text).toBe(WINDOW_PROMPT)
  }, 300_000)

  it('ask_session() refuses a classic session and sends it nothing', async () => {
    const { session } = await sessions.start(browser, { origin: 'chat', title: 'classic', mode: 'classic' })
    expect(session.mode).toBe('classic')
    const run = await flow('try:', `    await ask_session(${JSON.stringify(session.id)}, 'hi')`, 'except Exception as e:', '    return str(e)')
    expect(run.status).toBe('succeeded')
    expect(run.result).toContain(`session ${session.id} is classic; a flow talks only to a durable session`)
    expect(run.steps.map((s) => [s.fn, s.status])).toEqual([['ask_session', 'failed']])
    const events = await db.sql`SELECT 1 FROM ai_session_events WHERE session_id = ${session.id} AND event::jsonb->>'type' = 'user.turn'`
    expect(events).toHaveLength(0)
  }, 120_000)
})
