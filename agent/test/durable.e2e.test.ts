import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { CredentialStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { durableWorkflowId, TemporalDurableSessions } from '../src/durable/client.js'
import { kekFromBase64, type Kek } from '../src/secrets.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { RESUMED_FRESH } from '../src/sessions/manager.js'
import { SubjectPayloadCodec } from '../src/temporal/codec.js'
import { PgPayloadKeys } from '../src/temporal/payloadKeys.js'
import { PgSessionOwners, toolActivities } from '../src/temporal/toolActivities.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import type { ToolServices } from '../src/tools/registry.js'
import { Client } from '@temporalio/client'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { type LiveAgent, openPanelSocket, type PanelSocket, startLiveAgent } from './support/liveAgent.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI } from './support/temporal.js'

// A durable turn end to end (#1056): the panel's socket (routes/chat.ts) into the
// SessionManager, its update-with-start on Temporal, agent-durable's real worker
// (`python -m scadbuddy_durable.worker`, a child process) running the DurableSession
// workflow and the real Claude Code its SDK bundles against the fake Anthropic endpoint,
// each tool call as an activity on this service's agent-tools worker, and the
// projector's events back on the socket. Postgres holds the session, the credential
// (sealed under the KEK both services read), the payload keys and the snapshots.

const DURABLE_DIR_ENV = 'SCADBUDDY_TEST_AGENT_DURABLE'
const DURABLE_DIR = process.env[DURABLE_DIR_ENV]?.trim() || undefined

function uvOnPath(): string | undefined {
  try {
    return execFileSync('which', ['uv'], { encoding: 'utf8' }).trim() || undefined
  } catch {
    return undefined
  }
}
const UV = process.env.SCADBUDDY_TEST_UV?.trim() || uvOnPath()

const skip = !DURABLE_DIR
  ? `${DURABLE_DIR_ENV} is not set`
  : !TEST_DATABASE_URL
    ? 'SCADBUDDY_TEST_DATABASE_URL is not set'
    : !TEMPORAL_CLI
      ? 'no Temporal CLI'
      : !UV
        ? 'no uv (SCADBUDDY_TEST_UV or PATH)'
        : undefined

/** `pnpm build` writes both; the worker reads the prompt policy beside the manifest. */
const MANIFEST = fileURLToPath(new URL('../dist/tools.json', import.meta.url))
const PROMPT = fileURLToPath(new URL('../dist/durable-prompt.txt', import.meta.url))

const TOKEN = 'gw-durable-e2e-token-5555666677778888'
const NAMESPACE = 'default'
/** Generous: the clock on some hosts jumps, and a first segment starts the Claude Code binary. */
const TURN_MS = 120_000

type Frame = Record<string, unknown>
const is = (type: string) => (f: Frame) => f.type === type
const status = (s: string) => (f: Frame) => f.type === 'session.status' && f.status === s

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as { port: number }
  await new Promise<void>((resolve) => server.close(() => resolve()))
  return port
}

/** The model's side of the agent loop only: Claude Code's side queries (the title) offer no tools. */
const loopCalls = (fake: FakeAnthropic) => fake.messageCalls().filter((r) => r.body?.tools?.length)
/** Whether the conversation holds a tool result yet (Claude Code adds a system message after it). */
const answered = (r: RecordedRequest) => JSON.stringify(r.body?.messages ?? []).includes('"tool_result"')

describe.skipIf(skip !== undefined)(`a durable turn through agent-durable${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let env: TestWorkflowEnvironment
  let kekDir: string
  let kekFile: string
  let kek: Kek

  // Per test.
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let tools: AgentWorker
  let agent: LiveAgent
  let sessions: SessionManager
  let durableClient: Client
  let child: ChildProcess | undefined
  let childOutput: string
  let childExit: Promise<void>
  let home: string
  /** The get_settings backend call: answers at once unless `hangSettings`; then it waits for the release. */
  let hangSettings: boolean
  let settingsCalls: number
  let settingsHung: Promise<void>
  let markHung: () => void
  const releases: (() => void)[] = []

  beforeAll(async () => {
    if (!existsSync(MANIFEST) || !existsSync(PROMPT)) throw new Error(`run \`pnpm build\` first: ${MANIFEST} and ${PROMPT}`)
    env = await localTemporal()
    kekDir = await mkdtemp(path.join(os.tmpdir(), 'durable-e2e-kek-'))
    kekFile = path.join(kekDir, 'kek')
    const encoded = randomBytes(32).toString('base64')
    await writeFile(kekFile, `${encoded}\n`, { mode: 0o600 })
    kek = kekFromBase64(encoded)
  }, 90_000)
  afterAll(async () => {
    await env?.teardown()
    if (kekDir) await rm(kekDir, { recursive: true, force: true })
  })

  beforeEach(async () => {
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    fake = await startFakeAnthropic((r) => (r.body?.tools?.length ? script(r) : { text: 'Side reply' }))
    await new CredentialStore(db.sql).create({ kind: 'gateway', base_url: fake.url, secret: TOKEN }, kek)

    hangSettings = false
    settingsCalls = 0
    settingsHung = new Promise((resolve) => {
      markHung = resolve
    })
    const backendFetch: typeof fetch = async (input) => {
      const url = typeof input === 'object' && 'url' in input ? input.url : String(input)
      if (new URL(url).pathname === '/api/v1/settings') {
        settingsCalls++
        if (hangSettings) {
          markHung()
          await new Promise<void>((resolve) => releases.push(resolve))
        }
        return Response.json({ bambuddy_url: 'http://bambuddy.test', has_api_key: false })
      }
      return Response.json({ detail: 'not in this test' }, { status: 404 })
    }
    const payloadKeys = new PgPayloadKeys(db.sql, { current: kek })
    const dataConverter = { payloadCodecs: [new SubjectPayloadCodec(payloadKeys)] }
    const services: ToolServices = {
      backend: createBackendClient('http://backend.test', backendFetch),
      pending: new PendingActionStore(),
      pollIntervalMs: 1000,
      renderWaitMs: 60_000,
      operationFollowMs: 60_000,
      publicBaseUrl: undefined,
    }
    tools = AgentWorker.start({
      address: env.address,
      namespace: NAMESPACE,
      activities: toolActivities(ALL_TOOLS, { services, sessions: new PgSessionOwners(db.sql) }),
      dataConverter,
      shutdownGraceMs: 1_000,
      shutdownForceMs: 5_000,
      log: () => {},
    })
    await tools.running(60_000)
    durableClient = new Client({ connection: env.connection, namespace: NAMESPACE, dataConverter })
    const paths = await tempPaths()
    sessions = manager({
      sql: db.sql,
      paths,
      payloadKeys,
      durable: new TemporalDurableSessions(durableClient, db.sql),
      approvalPollMs: 50,
    })
    agent = await startLiveAgent(sessions)
    home = await mkdtemp(path.join(os.tmpdir(), 'durable-e2e-home-'))
    child = undefined
    childOutput = ''
  }, 90_000)

  afterEach(async () => {
    for (const release of releases.splice(0)) release()
    // Every session workflow this test left open (an abandoned one would keep its activities).
    for await (const wf of durableClient.workflow.list({ query: 'ExecutionStatus = "Running"' })) {
      if (wf.workflowId.startsWith('session-')) {
        await durableClient.workflow.getHandle(wf.workflowId).terminate('test over').catch(() => undefined)
      }
    }
    if (child && child.exitCode === null) {
      child.kill('SIGTERM')
      await childExit
    }
    await agent?.close()
    await tools?.stop()
    await fake?.close()
    await drop()
    await rm(home, { recursive: true, force: true })
    // The credential reached the engine only through its per-segment env.
    expect(childOutput).not.toContain(TOKEN)
  }, 90_000)

  /** agent-durable's worker, as its image runs it, with this test's database, Temporal and KEK. */
  async function startDurableWorker(): Promise<void> {
    const claude = path.join(home, 'claude')
    const cwd = path.join(home, 'srv-agent')
    await mkdir(claude, { recursive: true })
    await mkdir(cwd, { recursive: true })
    // The engine inherits the worker's environment: nothing of this machine's Claude or ScadBuddy.
    const inherited = Object.fromEntries(
      Object.entries(process.env).filter(([k]) => !/^(ANTHROPIC_|CLAUDE_|SCADBUDDY_)/.test(k)),
    )
    const url = new URL(TEST_DATABASE_URL!)
    url.searchParams.set('options', `-csearch_path=${schema}`)
    child = spawn(UV!, ['run', '--frozen', '--project', DURABLE_DIR!, 'python', '-m', 'scadbuddy_durable.worker'], {
      cwd,
      env: {
        ...inherited,
        // uv keeps its cache where it was; HOME moves for the engine's sake.
        UV_CACHE_DIR: process.env.UV_CACHE_DIR || path.join(os.homedir(), '.cache', 'uv'),
        // As the image: the package is not installed (`package = false`).
        PYTHONPATH: DURABLE_DIR!,
        HOME: home,
        CLAUDE_CONFIG_DIR: claude,
        SCADBUDDY_DATABASE_URL: url.toString(),
        SCADBUDDY_SECRET_KEY_FILE: kekFile,
        SCADBUDDY_TEMPORAL_ADDRESS: env.address,
        SCADBUDDY_TEMPORAL_NAMESPACE: NAMESPACE,
        SCADBUDDY_AGENT_DURABLE_HEALTH_PORT: String(await freePort()),
        SCADBUDDY_AGENT_TOOLS_MANIFEST: MANIFEST,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const proc = child
    proc.stdout!.on('data', (d: Buffer) => (childOutput += d.toString()))
    proc.stderr!.on('data', (d: Buffer) => (childOutput += d.toString()))
    childExit = new Promise((resolve) => proc.once('exit', () => resolve()))
  }

  /** Waits on the panel; on a timeout, says what the worker printed and what the model was last asked. */
  async function until(panel: PanelSocket, match: (f: Frame) => boolean, from = 0): Promise<Frame[]> {
    try {
      return await panel.until(match, { from, timeoutMs: TURN_MS })
    } catch (err) {
      const calls = loopCalls(fake).slice(-3).map((r) => JSON.stringify(r.body?.messages).slice(-3000))
      throw new Error(`${(err as Error).message}\n--- agent-durable ---\n${childOutput.slice(-6000)}\n--- calls ---\n${calls.join('\n\n')}`, {
        cause: err,
      })
    }
  }

  /** A new durable session's first message; `next` is the index after its `user.turn`. */
  async function newDurableSession(panel: PanelSocket, text: string): Promise<{ sessionId: string; next: number }> {
    await until(panel, is('sessions.snapshot'))
    const from = panel.frames.length
    panel.send({ v: 1, type: 'user.message', mode: 'durable', text, context: { route: '/' } })
    const frames = await until(panel, is('user.turn'), from)
    const started = frames.find(is('session.started'))
    expect(started).toMatchObject({ mode: 'durable' })
    return { sessionId: started!.sessionId as string, next: from + frames.length }
  }

  it('runs a turn from the socket: the worker starts after the message, and the reply still arrives', async () => {
    script = (r) =>
      answered(r)
        ? { text: 'Done.' }
        : { toolUse: { name: 'mcp__durable__get_settings', input: {} } }
    const panel = await openPanelSocket(agent)
    const { sessionId, next } = await newDurableSession(panel, 'Read my settings')
    // Review Focus 1: nothing polls `agent` until now.
    await startDurableWorker()
    const turn = panel.frames.slice(0, next + (await until(panel, status('idle'), next)).length)

    const order = [
      (f: Frame) => f.type === 'session.started' && f.mode === 'durable',
      is('user.turn'),
      status('running'),
      (f: Frame) => f.type === 'tool.call' && String(f.name).endsWith('get_settings'),
      (f: Frame) => f.type === 'tool.result' && f.ok === true,
      (f: Frame) => f.type === 'assistant.text.delta' && f.delta === 'Done.',
      is('session.result'),
      status('idle'),
    ]
    let at = -1
    for (const [i, match] of order.entries()) {
      const next = turn.findIndex((f, j) => j > at && match(f))
      expect(next, `step ${i} after ${JSON.stringify(turn.slice(0, at + 1).map((f) => f.type))} in ${JSON.stringify(turn.slice(at + 1))}`).toBeGreaterThan(at)
      at = next
    }
    expect(turn.filter((f) => f.type === 'error' && f.code !== 'worker_pending')).toEqual([])
    expect(settingsCalls).toBe(1)
    expect(loopCalls(fake)).toHaveLength(2)
    expect(JSON.stringify(loopCalls(fake)[0]!.body?.messages)).toContain('Read my settings')
    expect(fake.messageCalls().every((r) => r.headers.authorization === `Bearer ${TOKEN}`)).toBe(true)
    // The history holds ciphertext only, and never the credential.
    const raw = JSON.stringify(await env.client.workflow.getHandle(durableWorkflowId(sessionId)).fetchHistory())
    expect(raw).not.toContain(TOKEN)
    expect(raw).not.toContain('Read my settings')
    await expectPanelAccepts(panel.frames)
    panel.close()
  }, 240_000)

  it('asks for an outward call over the socket, and a denial reaches the model as a failed result', async () => {
    script = (r) =>
      answered(r)
        ? { text: 'Not deleted.' }
        : { toolUse: { name: 'mcp__durable__delete_model', input: { slug: 'keychain' } } }
    await startDurableWorker()
    const panel = await openPanelSocket(agent)
    const { sessionId } = await newDurableSession(panel, 'Delete the keychain')
    const parked = await until(panel, is('approval.required'))
    const required = parked.at(-1)!
    expect(String(required.id)).toMatch(new RegExp(`^durable:${sessionId}:`))
    await until(panel, status('waiting_approval'))

    const mark = panel.frames.length
    panel.send({ v: 1, type: 'approval.decision', sessionId, id: required.id, approve: false })
    const rest = await until(panel, status('idle'), mark)
    const resolved = rest.findIndex(is('approval.resolved'))
    const result = rest.findIndex(is('tool.result'))
    expect(rest[resolved]).toMatchObject({ id: required.id, approved: false, by: browser })
    expect(rest[result]).toMatchObject({ ok: false })
    expect(resolved).toBeLessThan(result)
    expect(rest.filter(is('assistant.text.delta')).map((f) => f.delta).join('')).toBe('Not deleted.')
    await expectPanelAccepts(panel.frames)
    panel.close()
  }, 240_000)

  it('resumes after a Stop: the next message sees the first one and the stopped call', async () => {
    let second = false
    script = () =>
      second ? { text: 'Second answer.' } : { toolUse: { name: 'mcp__durable__get_settings', input: {} } }
    hangSettings = true
    await startDurableWorker()
    const panel = await openPanelSocket(agent)
    const { sessionId } = await newDurableSession(panel, 'First question')
    await until(panel, is('tool.call'))
    await settingsHung

    const mark = panel.frames.length
    panel.send({ v: 1, type: 'session.interrupt', sessionId })
    await until(panel, status('idle'), mark)

    second = true
    const before = loopCalls(fake).length
    const next = panel.frames.length
    panel.send({ v: 1, type: 'user.message', sessionId, text: 'Second question', context: { route: '/' } })
    const turn = await until(panel, status('idle'), next)
    expect(turn.filter(is('assistant.text.delta')).map((f) => f.delta).join('')).toBe('Second answer.')
    const asked = loopCalls(fake).slice(before)
    expect(asked.length).toBeGreaterThan(0)
    const seen = JSON.stringify(asked[0]!.body?.messages)
    expect(seen).toContain('First question')
    expect(seen).toContain('Second question')
    expect(seen).toMatch(/"tool_result".*interrupted/i)
    panel.close()
  }, 240_000)

  it('resumes after a terminate from the snapshot, with the started call interrupted', async () => {
    let second = false
    script = () =>
      second ? { text: 'After the restore.' } : { toolUse: { name: 'mcp__durable__get_settings', input: {} } }
    hangSettings = true
    await startDurableWorker()
    const panel = await openPanelSocket(agent)
    const { sessionId } = await newDurableSession(panel, 'First question')
    await until(panel, is('tool.call'))
    await settingsHung
    // The snapshot that holds the started call is saved before the call is scheduled.
    const mark = panel.frames.length
    // As an operator would.
    await env.client.workflow.getHandle(durableWorkflowId(sessionId)).terminate('operator')
    // The projector settles a session whose run closed.
    await until(panel, status('idle'), mark)

    second = true
    const before = loopCalls(fake).length
    const next = panel.frames.length
    panel.send({ v: 1, type: 'user.message', sessionId, text: 'Second question', context: { route: '/' } })
    const turn = await until(panel, status('idle'), next)
    expect(turn.filter(is('assistant.text.delta')).map((f) => f.delta).join('')).toBe('After the restore.')
    const asked = loopCalls(fake).slice(before)
    expect(asked.length).toBeGreaterThan(0)
    const seen = JSON.stringify(asked[0]!.body?.messages)
    expect(seen).toContain('First question')
    expect(seen).toMatch(/"tool_result".*interrupted/i)
    const errors = await db.sql<{ event: string }[]>`
      SELECT event FROM ai_session_events WHERE session_id = ${sessionId} AND event LIKE '%resumed_fresh%'`
    expect(errors).toEqual([])
    expect(panel.frames.some((f) => f.type === 'error' && f.message === RESUMED_FRESH)).toBe(false)
    panel.close()
  }, 240_000)
})
