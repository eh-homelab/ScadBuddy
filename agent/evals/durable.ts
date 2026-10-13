import type { Sql } from 'postgres'
import { Client } from '@temporalio/client'
import { Context } from '@temporalio/activity'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createBackendClient } from '../src/api/backend.js'
import { AuditLog } from '../src/audit/log.js'
import { type Credential, CredentialStore, SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { OWN_PLUGIN_DIR } from '../src/harness/ownPlugin.js'
import type { RiskTier } from '../src/harness/permissions.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { kekFromBase64 } from '../src/secrets.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { SessionManager, SETTING_MODEL, SETTING_SESSION_BUDGET_USD } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'
import { PgPayloadKeys, SubjectPayloadCodec } from '../src/temporal/payloadCodec.js'
import { gateActivities, PgApprovalRecords, PgSessionOwners, toolActivities, type ToolActivity } from '../src/temporal/toolActivities.js'
import { turnToolActivities, turnToolActivity, type TurnToolsDeps } from '../src/temporal/turnTools.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { DURABLE_TOOLS, writeManifest } from '../src/tools/manifest.js'
import { PendingActionStore } from '../src/tools/pending.js'
import type { ToolServices } from '../src/tools/registry.js'
import { fixedCredentials } from '../test/support/fixedCredentials.js'
import { ensureTemplate, TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from '../test/support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_CLI_ENV } from '../test/support/temporal.js'
import { EVAL_BACKEND_URL, EvalBackend } from './backend.js'
import { collectRenders, type CollectOptions, type ScenarioBackend } from './render.js'
import { DURABLE_EVAL_TOOL_PREFIX as DURABLE_TOOL_PREFIX, type Outcome, type RunResult, type Scenario, type ScriptedTurn, type ToolCall } from './runner.js'

// The evals in a durable session (#2320): the same scenarios and checks as the classic
// run (runner.ts `runScenario`), answered through the stack a durable chat runs on. A
// Temporal dev server; agent-durable's real worker (`python -m scadbuddy_durable`, its
// real DurableSession and the Claude Code its SDK bundles, no scripted model); the
// agent's own agent-tools worker serving the real tool registry (DURABLE_TOOLS) against
// the scenario's backend; and SessionManager starting the session in durable mode, so
// the turn gets what a durable chat's turn gets (its system append included). The
// credential is saved in Settings as a session's is, so a gateway pointed at the fake
// Anthropic endpoint (test/evals.durable.e2e.test.ts) or a live key both work.
//
// Every parked outward call is denied through the gate's `respond` Update, as the
// browser user would deny it. The model then gets the durable engine's own rejection
// text (DURABLE_REJECTION), followed by a classic turn's refusal wording, which the
// segment runner adds (#2392); the outcome records the engine's text.
//
// The outcome is built from what the session logged (`ai_session_events`, what the
// panel shows) and from the tool activities themselves: a durable tool.result event
// carries the call's status, not its text, so each activity's return (or failure) is
// recorded where it runs.

/** What the durable engine tells the model a denied call came back with (temporalio.claude_agent_sdk `_workflow.py`). */
export const DURABLE_REJECTION = 'A human reviewer rejected this action. Do not retry it.'

const DURABLE_DIR = fileURLToPath(new URL('../../agent-durable/', import.meta.url))

/** Who starts an eval's session: the browser user, with every tier, as runScenario's principal. */
const EVAL_OWNER: Owner = { kind: 'browser', id: 'browser', label: 'Eval' }

function hasUv(): boolean {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

/** Why the durable run cannot happen here, or undefined when it can. */
export function durableEvalSkip(): string | undefined {
  if (!hasUv()) return 'uv is not on PATH (agent-durable runs under it)'
  if (!TEMPORAL_CLI) return `${TEMPORAL_CLI_ENV} is not set and no temporal on PATH`
  if (!TEST_DATABASE_URL) return `${TEST_DATABASE_URL_ENV} is not set`
  return undefined
}

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as { port: number }
  await new Promise<void>((resolve) => server.close(() => resolve()))
  return port
}

function withSearchPath(url: string, schema: string): string {
  const u = new URL(url)
  u.searchParams.set('options', `-csearch_path=${schema}`)
  return u.toString()
}

function storedCredential(c: Credential) {
  return c.kind === 'gateway' ? { kind: c.kind, base_url: c.baseUrl, secret: c.secret } : { kind: c.kind, secret: c.secret }
}

export type DurableStackOptions = {
  credential: Credential
  /** Model id or alias, saved as the `model` setting the durable runner reads. */
  model?: string
  /** Each session's budget (the `session_max_budget_usd` setting); 0.5, as runScenario's. */
  maxBudgetUsd?: number
  /** How long render_model waits for a render before handing back the running job. */
  renderWaitMs?: number
  /** The durable worker's output, line by line. */
  stderr?: (line: string) => void
  /** A turn's other tools (turnServers.ts), built on the stack's database and state dir; none by default. */
  turnServers?: (deps: { sql: Sql; stateDir: string }) => TurnToolsDeps['servers']
  /** How a parked approval is answered; every one is denied by default, as a careful user would. */
  decide?: (tool: string) => 'approve' | 'deny'
}

export type DurableRunOptions = {
  /** Whole-run deadline. */
  timeoutMs?: number
  collect?: CollectOptions
}

/** A tool activity's own record of one call, keyed by its tool_use id. */
type Ran = { ok: boolean; result: string }

export type DurableEvalStack = {
  run(scenario: Scenario, options?: DurableRunOptions, backend?: ScenarioBackend): Promise<Outcome & { script: ScriptedTurn[]; sessionId?: string }>
  close(): Promise<void>
}

/** Starts the durable stack once; each `run` is a new durable session on it. */
export async function startDurableStack(options: DurableStackOptions): Promise<DurableEvalStack> {
  const skip = durableEvalSkip()
  if (skip) throw new Error(`the durable eval cannot run: ${skip}`)
  const cleanups: (() => Promise<unknown>)[] = []
  const close = async () => {
    for (const cleanup of cleanups.reverse()) await cleanup().catch(() => {})
  }
  try {
    const env: TestWorkflowEnvironment = await localTemporal()
    cleanups.push(() => env.teardown())
    await ensureTemplate()
    const { db, url, schema, drop } = await throwawayDatabase()
    cleanups.push(drop)
    if (!(await db.ready())) throw new Error('the eval database did not migrate')
    const dir = await mkdtemp(path.join(os.tmpdir(), 'scadbuddy-durable-eval-'))
    cleanups.push(() => rm(dir, { recursive: true, force: true }))
    await ensureStateDirs({ stateDir: dir })
    const kekB64 = randomBytes(32).toString('base64')
    await writeFile(path.join(dir, 'kek'), kekB64, { mode: 0o600 })
    const kek = kekFromBase64(kekB64)
    await writeManifest(path.join(dir, 'tools.json'))

    // What a session's turn reads: the credential, the model and the budget, in Settings.
    await new CredentialStore(db.sql).put(storedCredential(options.credential), kek)
    const settings = new SettingsStore(db.sql)
    if (options.model) await settings.set(SETTING_MODEL, options.model)
    await settings.set(SETTING_SESSION_BUDGET_USD, options.maxBudgetUsd ?? 0.5)

    const worker = await startDurableWorker({ url: withSearchPath(url, schema), address: env.address, dir, stderr: options.stderr })
    cleanups.push(() => stopProcess(worker))

    const codec = { payloadCodecs: [new SubjectPayloadCodec(new PgPayloadKeys(db.sql, kek))] }
    const client = new Client({ connection: env.connection, namespace: 'default', dataConverter: codec })

    // Each run's backend, behind one set of services the worker was started with.
    let current: ScenarioBackend = new EvalBackend()
    const ran = new Map<string, Ran>()
    const services = {
      backend: createBackendClient(EVAL_BACKEND_URL, (input, init) => current.fetch(input, init)),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: options.renderWaitMs ?? 5_000,
    } satisfies ToolServices
    const sessions = new PgSessionOwners(db.sql)
    const audit = new AuditLog({ sql: db.sql, settings: () => settings, hashKey: randomBytes(32) })
    const turnDeps: TurnToolsDeps = {
      sessions,
      approvals: new PgApprovalRecords(db.sql),
      audit,
      servers: options.turnServers?.({ sql: db.sql, stateDir: dir }) ?? (async () => ({ servers: [], problems: [], secrets: [], release: () => {} })),
    }
    const tools = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: {
        ...recording(toolActivities(DURABLE_TOOLS, { services, sessions, audit }), ran),
        ...gateActivities({ audit, sessions }),
        ...turnToolActivities(turnDeps),
        default: turnToolActivity(turnDeps),
      },
      dataConverter: codec,
    })
    cleanups.push(() => tools.stop())
    await tools.running(60_000)

    const manager = new SessionManager({
      sql: db.sql,
      paths: { stateDir: dir },
      credentials: fixedCredentials(options.credential),
      settings,
      newSessions: { max: 10_000, windowMs: 1 },
    })
    manager.durableTurns = new DurableTurns({ client, sql: db.sql, events: manager.events, sendTimeoutMs: 60_000 })
    const gate = new DurableGate(client, { timeoutMs: 15_000 })
    const started: string[] = []
    cleanups.push(async () => {
      for (const id of started) await client.workflow.getHandle(sessionWorkflowId(id)).terminate('eval over').catch(() => {})
    })

    return {
      close,
      async run(scenario, runOptions = {}, backend = new EvalBackend()) {
        current = backend
        ran.clear()
        const { prompt, script } = scenario.prepare(backend)
        const startedAt = Date.now()
        const deadline = startedAt + (runOptions.timeoutMs ?? 300_000)
        const approvals: Outcome['approvals'] = []
        let error: string | undefined
        let sessionId: string | undefined
        let log: Logged[] = []
        try {
          const { session } = await manager.start(EVAL_OWNER, { origin: 'mcp', title: scenario.id, mode: 'durable', prompt })
          sessionId = session.id
          started.push(session.id)
          const answered = new Set<string>()
          const polled: Polled = {}
          for (;;) {
            log = await events(db, session.id)
            if (ended(log)) break
            if (Date.now() > deadline) throw new Error(timedOut(polled))
            const pending = await pollPending(gate, session.id, polled)
            for (const entry of pending) {
              if (entry.kind !== 'approval' || answered.has(entry.id)) continue
              answered.add(entry.id)
              const toolUseId = entry.id.slice(entry.id.lastIndexOf(':') + 1)
              const call = log.find((e) => e.type === 'tool.call' && e.id === toolUseId)
              approvals.push({
                toolName: `${DURABLE_TOOL_PREFIX}${entry.tool}`,
                input: (call?.input as Record<string, unknown> | undefined) ?? {},
                toolUseId,
                tier: (call?.risk as RiskTier | undefined) ?? 'outward',
              })
              const decision = options.decide?.(entry.tool) ?? 'deny'
              await gate.respond(session.id, { request_id: entry.id, response: { kind: 'approval', decision }, responder: EVAL_OWNER, role: 'browser' })
            }
            await new Promise((r) => setTimeout(r, 200))
          }
        } catch (err) {
          error = err instanceof Error ? err.message : String(err)
        }
        const folded = foldEvents(log, ran)
        // Each approval's input, from the call's event when it was logged after the park.
        for (const a of approvals) {
          const call = folded.toolCalls.find((c) => c.id === a.toolUseId)
          if (call && Object.keys(a.input).length === 0) a.input = call.input
        }
        const renders = await collectRenders(backend, runOptions.collect)
        return {
          scenario: scenario.id,
          ...folded,
          approvals,
          decisions: folded.toolCalls.map((c) => ({
            toolName: c.name,
            decision: approvals.some((a) => a.toolUseId === c.id) ? ('needs_approval' as const) : ('allow' as const),
            tier: c.tier,
          })),
          backend,
          renders,
          ...(options.model ? { model: options.model } : {}),
          ...(error !== undefined ? { error: sessionId ? `${error} (session ${sessionId})` : error } : {}),
          durationMs: Date.now() - startedAt,
          script,
          ...(sessionId ? { sessionId } : {}),
        }
      },
    }
  } catch (err) {
    await close()
    throw err
  }
}

/** Records each tool activity's own result, by the tool_use id its activity id carries (`tool-<id>`). */
function recording(activities: Record<string, ToolActivity>, ran: Map<string, Ran>): Record<string, ToolActivity> {
  return Object.fromEntries(
    Object.entries(activities).map(([name, run]) => [
      name,
      async (input: unknown) => {
        const id = Context.current().info.activityId.replace(/^tool-/, '')
        try {
          const result = await run(input)
          ran.set(id, { ok: true, result: typeof result === 'string' ? result : JSON.stringify(result) })
          return result
        } catch (err) {
          ran.set(id, { ok: false, result: err instanceof Error ? err.message : String(err) })
          throw err
        }
      },
    ]),
  )
}

export type Logged = { type: string; [key: string]: unknown }

async function events(db: Database, sessionId: string): Promise<Logged[]> {
  const rows = await db.sql<{ event: string }[]>`SELECT event FROM ai_session_events WHERE session_id = ${sessionId} ORDER BY seq`
  return rows.map((r) => JSON.parse(r.event) as Logged)
}

/** finish_turn's events end the turn: its `session.result`, then a `session.status`. */
function ended(log: Logged[]): boolean {
  const result = log.findIndex((e) => e.type === 'session.result')
  return result >= 0 && log.slice(result).some((e) => e.type === 'session.status')
}

/** The session's events as an Outcome's tool calls, final text and result. */
/** What the last poll of the gate's pending input saw: its failure, while the gate keeps failing. */
export type Polled = { gateError?: string }

/**
 * The session's pending input, or none when the gate cannot be read (#2465). The poll
 * goes on, as a later read may answer, but the failure is kept: a gate that never
 * answers leaves a parked call undenied, and the run then ends only at its deadline.
 */
export async function pollPending(
  gate: Pick<DurableGate, 'pendingInput'>,
  sessionId: string,
  polled: Polled,
): Promise<Awaited<ReturnType<DurableGate['pendingInput']>>> {
  try {
    const pending = await gate.pendingInput(sessionId)
    delete polled.gateError
    return pending
  } catch (err) {
    polled.gateError = err instanceof Error ? err.message : String(err)
    return []
  }
}

/** The deadline's error, naming the gate's failure when the last poll had one. */
export function timedOut(polled: Polled): string {
  return polled.gateError === undefined ? 'eval timed out' : `eval timed out; the gate's pending input last failed: ${polled.gateError}`
}

export function foldEvents(log: Logged[], ran: ReadonlyMap<string, Ran>): Pick<Outcome, 'toolCalls' | 'finalText' | 'result'> {
  const toolCalls: ToolCall[] = []
  let finalText = ''
  const texts = new Map<string, string>()
  let result: RunResult | undefined
  // A turn's `error` and `session.result` are matched within the turn, which its
  // `session.status` closes, in either order. finish_turn logs the error after the
  // result today (agent-durable session/activities.py `turn_events`); a reorder must
  // not score a failed turn as a success (#2465).
  let turnError: string | undefined
  let turnResult = false
  for (const e of log) {
    if (e.type === 'tool.call' && typeof e.id === 'string' && typeof e.name === 'string') {
      toolCalls.push({
        id: e.id,
        name: `${DURABLE_TOOL_PREFIX}${e.name}`,
        tool: e.name,
        input: (e.input as Record<string, unknown> | undefined) ?? {},
        tier: (e.risk as RiskTier | undefined) ?? 'outward',
      })
    } else if (e.type === 'tool.result' && typeof e.id === 'string') {
      const call = toolCalls.find((c) => c.id === e.id)
      if (!call) continue
      call.ok = e.ok === true
      const own = ran.get(e.id)
      // A call that never reached its activity was refused before it: denied at the gate.
      call.result = own ? own.result : e.summary === 'rejected' ? DURABLE_REJECTION : String(e.summary ?? '')
    } else if (e.type === 'assistant.text.delta' && typeof e.messageId === 'string' && typeof e.delta === 'string') {
      const text = (texts.get(e.messageId) ?? '') + e.delta
      texts.set(e.messageId, text)
      if (text.trim()) finalText = text
    } else if (e.type === 'session.result') {
      result = {
        subtype: 'success',
        ...(typeof e.costUsd === 'number' ? { total_cost_usd: e.costUsd } : {}),
        ...(typeof e.turns === 'number' ? { num_turns: e.turns } : {}),
        ...(turnError !== undefined ? { subtype: turnError } : {}),
      }
      turnResult = true
    } else if (e.type === 'error') {
      turnError = `error: ${String(e.code)}: ${String(e.message ?? '')}`
      if (result && turnResult) result = { ...result, subtype: turnError }
    } else if (e.type === 'session.status') {
      turnError = undefined
      turnResult = false
    }
  }
  return { toolCalls, finalText, ...(result ? { result } : {}) }
}

async function startDurableWorker(o: { url: string; address: string; dir: string; stderr?: ((line: string) => void) | undefined }): Promise<ChildProcess> {
  const port = await freePort()
  const worker = spawn('uv', ['run', '--frozen', 'python', '-m', 'scadbuddy_durable'], {
    cwd: DURABLE_DIR,
    env: {
      ...process.env,
      SCADBUDDY_DATABASE_URL: o.url,
      SCADBUDDY_TEMPORAL_ADDRESS: o.address,
      SCADBUDDY_TEMPORAL_NAMESPACE: 'default',
      SCADBUDDY_SECRET_KEY_FILE: path.join(o.dir, 'kek'),
      SCADBUDDY_DURABLE_TOOLS_JSON: path.join(o.dir, 'tools.json'),
      SCADBUDDY_DURABLE_CWD: o.dir,
      // ScadBuddy's own plugin, as runScenario gives the classic harness (`ownPlugin`).
      SCADBUDDY_DURABLE_SKILLS_DIR: OWN_PLUGIN_DIR,
      SCADBUDDY_DURABLE_HEALTH_PORT: String(port),
      // No agent serves the subagents' loopback MCP server here: without "off" every
      // segment's Claude Code waits about 30 s on a server it cannot reach.
      SCADBUDDY_DURABLE_SUBAGENT_MCP_URL: 'off',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = ''
  const take = (d: Buffer) => {
    const text = d.toString()
    log = (log + text).slice(-20_000)
    if (o.stderr) for (const line of text.split('\n')) if (line) o.stderr(line)
  }
  worker.stdout?.on('data', take)
  worker.stderr?.on('data', take)
  // uv may sync the worker's environment first.
  const deadline = Date.now() + 240_000
  for (;;) {
    if (worker.exitCode !== null) throw new Error(`the durable worker exited: ${log}`)
    const status = await fetch(`http://127.0.0.1:${port}/healthz`)
      .then((r) => r.json() as Promise<{ worker?: string }>)
      .catch(() => undefined)
    if (status?.worker === 'running') return worker
    if (status?.worker && status.worker !== 'starting') {
      await stopProcess(worker)
      throw new Error(`the durable worker is ${status.worker}: ${log}`)
    }
    if (Date.now() > deadline) {
      await stopProcess(worker)
      throw new Error(`the durable worker did not start: ${log}`)
    }
    await new Promise((r) => setTimeout(r, 500))
  }
}

async function stopProcess(worker: ChildProcess): Promise<void> {
  if (worker.exitCode !== null) return
  const exited = new Promise((resolve) => worker.once('exit', resolve))
  worker.kill('SIGTERM')
  await Promise.race([exited, new Promise((r) => setTimeout(r, 15_000))])
  if (worker.exitCode === null) worker.kill('SIGKILL')
}
