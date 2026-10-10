import { Client } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { kekFromBase64 } from '../src/secrets.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { SETTING_SESSION_MODE } from '../src/sessions/manager.js'
import { PgPayloadKeys, SubjectPayloadCodec } from '../src/temporal/payloadCodec.js'
import { writeManifest } from '../src/tools/manifest.js'
import { expectPanelAccepts, frontendClientMessages } from './support/frontendProtocol.js'
import { type LiveAgent, openPanelSocket, type PanelSocket, startLiveAgent } from './support/liveAgent.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// A durable turn end to end (plan 5c Task 3.3): the panel's own client message over the
// chat socket, through SessionManager's dispatch (sessions/durable.ts), to agent-durable's
// real DurableSession workflow on a Temporal dev server, run by the real Python worker
// (`python -m scadbuddy_durable`) with its scripted model in place of Claude
// (SCADBUDDY_DURABLE_SCRIPTED=1, session/scripted.py: it answers "you said: <message>").
// The workflow's events reach ai_session_events, and the panel, through the log.
// Payloads are sealed by the subject codec on both sides. Skips without uv, Temporal
// or Postgres.

const DURABLE_DIR = fileURLToPath(new URL('../../agent-durable/', import.meta.url))

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

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as { port: number }
  await new Promise<void>((resolve) => server.close(() => resolve()))
  return port
}

/** The test schema in the database URL, for a client that takes no search_path setting. */
function withSearchPath(url: string, schema: string): string {
  const u = new URL(url)
  u.searchParams.set('options', `-csearch_path=${schema}`)
  return u.toString()
}

describe.skipIf(skip !== undefined)(`a durable turn from the chat socket${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let worker: ChildProcess | undefined
  let workerLog = ''
  let agent: LiveAgent | undefined
  let panel: PanelSocket | undefined
  let client: Client
  const sessionIds: string[] = []

  beforeAll(async () => {
    env = await localTemporal()
    let schema: string
    let url: string
    ;({ db, url, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const dir = await mkdtemp(path.join(os.tmpdir(), 'durable-e2e-'))
    const kekB64 = randomBytes(32).toString('base64')
    await writeFile(path.join(dir, 'kek'), kekB64, { mode: 0o600 })
    await writeManifest(path.join(dir, 'tools.json'))
    const port = await freePort()
    worker = spawn('uv', ['run', '--frozen', 'python', '-m', 'scadbuddy_durable'], {
      cwd: DURABLE_DIR,
      env: {
        ...process.env,
        SCADBUDDY_DATABASE_URL: withSearchPath(url, schema),
        SCADBUDDY_TEMPORAL_ADDRESS: env.address,
        SCADBUDDY_TEMPORAL_NAMESPACE: 'default',
        SCADBUDDY_SECRET_KEY_FILE: path.join(dir, 'kek'),
        SCADBUDDY_DURABLE_TOOLS_JSON: path.join(dir, 'tools.json'),
        SCADBUDDY_DURABLE_CWD: dir,
        SCADBUDDY_DURABLE_HEALTH_PORT: String(port),
        SCADBUDDY_DURABLE_SCRIPTED: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    worker.stdout?.on('data', (d: Buffer) => (workerLog += d.toString()))
    worker.stderr?.on('data', (d: Buffer) => (workerLog += d.toString()))
    // uv may sync the worker's environment first.
    const deadline = Date.now() + 240_000
    for (;;) {
      if (worker.exitCode !== null) throw new Error(`the durable worker exited: ${workerLog}`)
      const status = await fetch(`http://127.0.0.1:${port}/healthz`)
        .then((r) => r.json() as Promise<{ worker?: string }>)
        .catch(() => undefined)
      if (status?.worker === 'running') break
      if (status?.worker && status.worker !== 'starting') throw new Error(`the durable worker is ${status.worker}: ${workerLog}`)
      if (Date.now() > deadline) throw new Error(`the durable worker did not start: ${workerLog}`)
      await new Promise((r) => setTimeout(r, 500))
    }

    const kek = kekFromBase64(kekB64)
    client = new Client({
      connection: env.connection,
      namespace: 'default',
      dataConverter: { payloadCodecs: [new SubjectPayloadCodec(new PgPayloadKeys(db.sql, kek))] },
    })
    const sessions = manager({
      sql: db.sql,
      paths: await tempPaths(),
      settings: { get: <T>(key: string) => Promise.resolve((key === SETTING_SESSION_MODE ? 'durable' : undefined) as T) },
    })
    sessions.durableTurns = new DurableTurns({ client, sql: db.sql, events: sessions.events, sendTimeoutMs: 60_000 })
    agent = await startLiveAgent(sessions)
    panel = await openPanelSocket(agent)
  }, 300_000)

  afterAll(async () => {
    panel?.close()
    await agent?.close()
    for (const id of sessionIds) {
      await client
        ?.workflow.getHandle(sessionWorkflowId(id))
        .terminate('test over')
        .catch(() => {})
    }
    if (worker && worker.exitCode === null) {
      const exited = new Promise((resolve) => worker!.once('exit', resolve))
      worker.kill('SIGTERM')
      await Promise.race([exited, new Promise((r) => setTimeout(r, 15_000))])
      if (worker.exitCode === null) worker.kill('SIGKILL')
    }
    await drop?.()
    await env?.teardown()
  }, 60_000)

  it("runs a turn in the workflow and streams its reply to the panel, then the next one", async () => {
    const { clientMessage } = await frontendClientMessages()
    const p = panel!
    const is = (type: string, extra: (f: Record<string, unknown>) => boolean = () => true) => (f: Record<string, unknown>) =>
      f.type === type && extra(f)
    // The turn's end: the first `idle` after its reply (a new chat says `idle` before its
    // turn too), or an error, which fails the test with what it says.
    const idleAfterReply = (from: number) => (f: Record<string, unknown>) => {
      if (f.type === 'error') return true
      const done = p.frames.findIndex((g, i) => i >= from && g.type === 'assistant.text.done')
      return done >= 0 && p.frames.indexOf(f) > done && f.type === 'session.status' && f.status === 'idle'
    }

    await p.until(is('sessions.snapshot'))
    let from = p.frames.length
    p.send(clientMessage({ type: 'user.message', text: 'hello durable', context: { route: '/' } }))
    const first = await p.until(idleAfterReply(from), { from, timeoutMs: 120_000 })
    expect(first.filter(is('error'))).toEqual([])
    const started = first.find(is('session.started'))!
    const sessionId = started.sessionId as string
    sessionIds.push(sessionId)
    expect(first.find(is('user.turn'))).toMatchObject({ text: 'hello durable', author: browser })
    const reply = first.filter(is('assistant.text.delta')).map((f) => f.delta).join('')
    // The model got the page context after the words; the transcript's user.turn did not.
    expect(reply).toMatch(/^you said: hello durable\n\n<page_context>/)
    expect(first.filter(is('error'))).toEqual([])
    await expectPanelAccepts(first)
    const [row] = await db.sql<{ mode: string; status: string }[]>`SELECT mode, status FROM ai_sessions WHERE id = ${sessionId}`
    expect(row).toEqual({ mode: 'durable', status: 'idle' })

    // The same workflow takes the session's next message.
    from = p.frames.length
    p.send(clientMessage({ type: 'user.message', sessionId, text: 'and again', context: { route: '/' } }))
    const second = await p.until(idleAfterReply(from), { from, timeoutMs: 120_000 })
    expect(second.filter(is('error'))).toEqual([])
    expect(second.filter(is('assistant.text.delta')).map((f) => f.delta).join('')).toMatch(/^you said: and again\n/)

    // Sealed: the server's history holds none of the user's words.
    const { history } = await env.client.workflowService.getWorkflowExecutionHistory({
      namespace: 'default',
      execution: { workflowId: sessionWorkflowId(sessionId) },
    })
    const raw = Buffer.from(JSON.stringify(history)).toString()
    expect(raw).not.toContain(Buffer.from('hello durable').toString('base64').slice(0, 12))
  }, 300_000)
})
