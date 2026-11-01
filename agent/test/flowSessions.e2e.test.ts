import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import type { Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { respond } from '../src/routes/pendingInput.js'
import { kekFromBase64 } from '../src/secrets.js'
import { type SessionManager } from '../src/sessions/manager.js'
import { PgFlowRuns } from '../src/temporal/flowRuns.js'
import { flowSessionActivities } from '../src/temporal/flowSessions.js'
import { PgPayloadKeys, SubjectPayloadCodec } from '../src/temporal/payloadCodec.js'
import { gateActivities, PgApprovalRecords, PgSessionOwners, toolActivities } from '../src/temporal/toolActivities.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { turnToolActivities } from '../src/temporal/turnTools.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import type { ToolServices } from '../src/tools/registry.js'
import { AGENT_SESSIONS_SKIP, agentSessions, agentSessionsEnv } from './support/agentSessions.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'

// A flow's `agent(...)` and `ask_session(...)` end to end (#1057, plan
// 2026-10-09-durable-phase-6-flows.md §8: "agent(...) and ask_session against the fake
// endpoint"). Every piece is the real one, on one Temporal dev server and one schema:
//
// - the backend's `projects` worker runs the script in ProjectWorkflow
//   (backend/tests/flows/agent_e2e_driver.py, a process);
// - this process's flow tools worker serves `sessions_start`/`sessions_send` (the
//   SessionManager over DurableTurns) and `flow_session_check`/`flow_session_turn`, as
//   main.ts wires the agent-tools worker, on a queue of its own (the session stack's
//   agent-tools worker is support/agentSessions.ts's);
// - the session runs as an AgentSession (support/agentSessions.ts, as a worker replica runs
//   it), its turn on the real SDK and its bundled Claude Code, pointed as a gateway at this
//   process's fake Anthropic endpoint (support/fakeAnthropic.ts), which scripts the turn. Its
//   ScadBuddy tools are the turn's own, in-process, and its outward call parks in the turn's
//   gate (ai_pending_input).
//
// backend/tests/flows/test_agent_host_functions.py covers the host functions against
// stand-in activities, and flowSessions.pg.test.ts the two activities against a
// hand-written event log; this is the path between them. Skips without uv, Temporal,
// Postgres or the bundled Claude Code.

const BACKEND_DIR = fileURLToPath(new URL('../../backend/', import.meta.url))
const FAILURE_CONVERTER = fileURLToPath(new URL('../src/temporal/failureConverter.ts', import.meta.url))
const TOOLS_QUEUE = 'flow-tools-e2e'
const TOKEN = 'gateway-token-1057'
// A turn offers each ScadBuddy tool to Claude Code as `mcp__scadbuddy__<name>`.
const SCADBUDDY = 'mcp__scadbuddy__'
const TURN_TOOLS = ALL_TOOLS.filter((t) => t.name === 'sessions_list' || t.name === 'delete_output')
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

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const skip = !hasUv()
  ? 'uv is not on PATH'
  : AGENT_SESSIONS_SKIP
    ? AGENT_SESSIONS_SKIP.trim()
    : !TEST_DATABASE_URL
      ? `${TEST_DATABASE_URL_ENV} is not set`
      : cliMissing

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
  if (!tools.includes(`${SCADBUDDY}delete_output`)) return { text: 'ok' }
  const conversation = JSON.stringify(request.body?.messages ?? [])
  if (conversation.includes(WINDOW_PROMPT)) return { text: 'The window is 60 wide.' }
  if (!conversation.includes(SIZE_PROMPT)) return { text: 'ok' }
  const results = toolResults(request)
  if (results === 0) return { toolUse: { name: `${SCADBUDDY}sessions_list`, input: {} } }
  if (results === 1) return { toolUse: { name: `${SCADBUDDY}delete_output`, input: { output_id: 'out-1' } } }
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
  agentSessions()
  let db: Database
  let drop: () => Promise<void>
  let fake: FakeAnthropic
  let projects: Proc | undefined
  let worker: AgentWorker | undefined
  let services: ToolServices
  let sessions: SessionManager
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
    const env = agentSessionsEnv()
    let schema: string
    let url: string
    ;({ db, url, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const dir = await mkdtemp(path.join(os.tmpdir(), 'flow-sessions-e2e-'))
    const kekB64 = randomBytes(32).toString('base64')
    const kekFile = path.join(dir, 'kek')
    await writeFile(kekFile, kekB64, { mode: 0o600 })
    const kek = kekFromBase64(kekB64)
    fake = await startFakeAnthropic(reply)
    const databaseUrl = withSearchPath(url, schema)

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

    // This process: the sessions (AgentSessions on the stack's workers), and the flow tools
    // worker as main.ts wires the agent-tools worker.
    const codec = { payloadCodecs: [new SubjectPayloadCodec(new PgPayloadKeys(db.sql, kek))] }
    services = {
      backend: createBackendClient('http://backend.invalid', async () => new Response(null, { status: 503 })),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: 5_000,
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
        ...toolActivities(ALL_TOOLS, {
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

    // The projects worker up (uv may sync its environment first).
    const deadline = Date.now() + 240_000
    for (;;) {
      if (projects.child.exitCode !== null) throw new Error(`the projects worker exited: ${projects.log()}`)
      if (lines.includes('{"ready": true}')) break
      if (Date.now() > deadline) throw new Error(`the projects worker never got ready: ${projects.log()}`)
      await new Promise((r) => setTimeout(r, 500))
    }
  }, 300_000)

  // A manager per test: the session stack serves the managers of the running test only.
  beforeEach(async () => {
    sessions = manager({
      sql: db.sql,
      paths: await tempPaths(),
      settings: { get: <T>() => Promise.resolve(undefined as T) },
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      ...harnessTools(services, TURN_TOOLS),
      approvalHashKey: Buffer.alloc(32, 7),
    })
    services.sessions = sessions
  })

  afterAll(async () => {
    projects?.child.stdin?.end()
    await stop(projects)
    await worker?.stop()
    await stopManagers()
    await fake?.close()
    await drop?.()
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
        expect(await respond(sessions, browser, parked.request_id, { kind: 'approval', decision: 'deny' })).toMatchObject({ outcome: 'denied' })
        return parked.tool
      }
      if (Date.now() > deadline) throw new Error('no outward call parked')
      await new Promise((r) => setTimeout(r, 200))
    }
  }

  it("agent() starts a session, waits for its turn and parses the answer its result_schema asks for", async () => {
    const running = flow(`answer = await agent(${JSON.stringify(SIZE_PROMPT)}, [], ${JSON.stringify(SCHEMA)})`, "return answer['width'] + 2")
    // A run that ends without parking a call fails below with its own result.
    const [run, denied] = await Promise.all([running, denyTheOutwardCall(running)])
    expect(denied).toBe(`${SCADBUDDY}delete_output`)
    // The fenced JSON was parsed and checked against the schema: the script did arithmetic on it.
    expect([run.status, run.result]).toEqual(['succeeded', 'result: 42'])
    const [step] = run.steps
    expect(run.steps).toHaveLength(1)
    expect(step).toMatchObject({ fn: 'agent', status: 'succeeded', outward: false })
    const sessionId = step!.session_id!
    const [row] = await db.sql<{ runtime: string; owner_id: string }[]>`SELECT runtime, owner_id FROM ai_sessions WHERE id = ${sessionId}`
    expect(row).toEqual({ runtime: 'unified', owner_id: 'browser' })

    // The model was asked for that JSON, in the session the flow started.
    const asked = fake.messageCalls().find((r) => JSON.stringify(r.body?.messages ?? []).includes(SIZE_PROMPT))
    expect(JSON.stringify(asked?.body?.messages)).toContain('answer with only a JSON value that matches this JSON Schema')

    // The turn's one outward call (the denied delete_output, not the sessions_list read)
    // was counted: a Reset to before the step would make it again.
    const calls = await db.sql<{ event: { type: string; name: string; risk: string } }[]>`
      SELECT event::jsonb AS event FROM ai_session_events
       WHERE session_id = ${sessionId} AND event::jsonb->>'type' = 'tool.call' ORDER BY seq`
    expect(calls.map((c) => [c.event.name, c.event.risk])).toEqual([
      [`${SCADBUDDY}sessions_list`, 'read'],
      [`${SCADBUDDY}delete_output`, 'outward'],
    ])
    expect(run.preview).toEqual([['agent', step!.call_id]])
  }, 300_000)

  it('ask_session() sends to a session the starter owns and answers with its turn', async () => {
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
})
