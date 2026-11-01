import { randomBytes, randomUUID } from 'node:crypto'
import { mkdtemp, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { Client, WorkflowNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode } from '@temporalio/worker'
import { createBackendClient } from '../src/api/backend.js'
import { approvalHashKey } from '../src/approvals/service.js'
import { AuditLog } from '../src/audit/log.js'
import { type Credential, SettingsStore } from '../src/credentials.js'
import { connectDatabase, type Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { DurableGate, sessionWorkflowId } from '../src/gate/durable.js'
import { parseRequestId } from '../src/gate/ids.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { OWN_PLUGIN_DIR } from '../src/harness/ownPlugin.js'
import { runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { respond } from '../src/routes/pendingInput.js'
import { queuesPolled } from '../src/runtime/health.js'
import { roleParts } from '../src/runtime/role.js'
import { kekFromBase64 } from '../src/secrets.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { DurableTurns } from '../src/sessions/durable.js'
import { type QueryRunner, SessionManager, SETTING_MODEL, SETTING_SESSION_BUDGET_USD } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'
import { readRuntimeSettings } from '../src/sessions/runtimeSettings.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { TASK_QUEUE } from '../src/temporal/names.js'
import { FilePayloadStore, PgPayloadKeys, subjectDataConverter } from '../src/temporal/payloadCodec.js'
import { acquireActivities, releaseActivities } from '../src/temporal/session/affinity.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { sessionGateActivities } from '../src/temporal/session/gateActivities.js'
import { type HostWorkers, startHostWorkers } from '../src/temporal/session/hostWorkers.js'
import { SESSION_QUEUE, SESSION_SETTINGS_ACTIVITY, sessionHostQueue } from '../src/temporal/session/names.js'
import { sessionTurnActivity } from '../src/temporal/session/turnActivity.js'
import { gateActivities, PgApprovalRecords, PgSessionOwners } from '../src/temporal/toolActivities.js'
import { turnToolActivities, turnToolActivity, type TurnToolsDeps } from '../src/temporal/turnTools.js'
import { AgentWorker, workerIdentity } from '../src/temporal/worker.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import type { ToolServices } from '../src/tools/registry.js'
import { fixedCredentials } from '../test/support/fixedCredentials.js'
import { ensureTemplate, TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from '../test/support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_CLI_ENV } from '../test/support/temporal.js'
import { EVAL_BACKEND_URL, EvalBackend } from './backend.js'
import { collectRenders, type ScenarioBackend } from './render.js'
import { type CollectBudget, collectWithin, foldMessages, type Outcome, type Scenario, type ScriptedTurn } from './runner.js'

// The evals on the one runtime (plan 2026-10-13 Step 6.6, spec §3 goal 7): each scenario is a
// session, an AgentSession on Temporal, as every session has been since Step 6.3. Two agent
// processes on one Temporal dev server and one throwaway Postgres database, each wired as
// main.ts wires its SCADBUDDY_AGENT_ROLE (runtime/role.ts `roleParts`): the `api` process starts
// the session and answers its gate, as the panel does, and polls no turn queue; the `worker`
// process runs AgentSession, its shared agent-tools activities and its session host queue, so
// every turn runs there, in the real Claude Code, with the real ScadBuddy tools
// (tools/harness.ts `harnessTools`) against the scenario's backend and ScadBuddy's own plugin,
// as main.ts gives a session. Every payload is sealed, as main.ts seals them with the KEK. The
// credential is the caller's (a gateway pointed at the fake Anthropic endpoint, or a live key),
// the model and the budget are the Settings a session's turn reads.
//
// Every parked outward call is denied through the pending-input route's `respond`, as the
// browser user denies one in the panel; the model then gets the session's refusal ("The user
// denied ..."). The outcome is folded from the worker's Claude Code message stream
// (runner.ts `foldMessages`), so it records what the model saw, its init message included,
// and its decisions are the tier seam's own (HarnessRun `onDecision`).

/** Who starts an eval's session: the browser user, with every tier. */
const EVAL_OWNER: Owner = { kind: 'browser', id: 'browser', label: 'Eval' }

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))

/** Why the evals cannot run here, or undefined when they can. */
export function evalStackSkip(): string | undefined {
  if (!TEMPORAL_CLI) return `${TEMPORAL_CLI_ENV} is not set and no temporal on PATH`
  if (!TEST_DATABASE_URL) return `${TEST_DATABASE_URL_ENV} is not set`
  try {
    bundledCliPath()
  } catch (err) {
    return (err as Error).message
  }
  return undefined
}

export type EvalStackOptions = {
  credential: Credential
  /** Model id or alias, saved as the `model` setting a session's turn reads. */
  model?: string
  /** Each session's budget (the `session_max_budget_usd` setting); 0.5 by default. */
  maxBudgetUsd?: number
  /** How long render_model waits for a render before handing back the running job (5 s: the recorded backend's are instant). */
  renderWaitMs?: number
  /** Claude Code's stderr, line by line. */
  stderr?: (line: string) => void
}

export type EvalRunOptions = CollectBudget & {
  /** The turn's deadline. */
  timeoutMs?: number
}

export type EvalStack = {
  run(scenario: Scenario, options?: EvalRunOptions, backend?: ScenarioBackend): Promise<Outcome & { script: ScriptedTurn[]; sessionId?: string }>
  /** The type, task queue and status of `session-<id>`'s latest run. */
  workflow(sessionId: string): Promise<{ type: string; taskQueue: string; status: string }>
  /** How many Claude Code processes each process spawned. */
  spawned(): { api: number; worker: number }
  close(): Promise<void>
}

/** What the Claude Code a run's session spawned yielded, and the seam's decisions on its calls. */
type Sink = { messages: SDKMessage[]; decisions: Outcome['decisions'] }

/** One agent process of a role, as main.ts builds it. */
type AgentProcess = { m: SessionManager; spawned: number; stop(): Promise<void> }

/** What the last poll of the gate's pending input saw: its failure, while the gate keeps failing. */
export type Polled = { gateError?: string }

/**
 * The session's pending input, or none when the gate cannot be read (#2465). The poll goes on,
 * as a later read may answer, but the failure is kept: a gate that never answers leaves a parked
 * call undenied, and the run then ends only at its deadline.
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

/**
 * A warm query whose messages `seen` also gets, as Claude Code yields them; its interrupt and
 * close are the query's own, so the warm cache uses it as it is (manager.ts `startedQuery`).
 */
function tapped(query: ReturnType<typeof runHarness>, seen: (message: SDKMessage) => void) {
  return {
    [Symbol.asyncIterator]: () => {
      const it = query[Symbol.asyncIterator]()
      return {
        next: async () => {
          const next = await it.next()
          if (!next.done) seen(next.value)
          return next
        },
        return: async (value?: unknown) => (it.return ? it.return(value as never) : { done: true as const, value: undefined }),
      }
    },
    interrupt: () => query.interrupt(),
    close: () => query.close(),
  }
}

/** Starts the stack once; each `run` is a new session on it. */
export async function startEvalStack(options: EvalStackOptions): Promise<EvalStack> {
  const skip = evalStackSkip()
  if (skip) throw new Error(`the evals cannot run: ${skip}`)
  const cleanups: (() => Promise<unknown>)[] = []
  const close = async () => {
    for (const cleanup of cleanups.reverse()) await cleanup().catch(() => {})
  }
  try {
    const env: TestWorkflowEnvironment = await localTemporal()
    cleanups.push(() => env.teardown())
    const bundle = await bundleWorkflowCode({ workflowsPath: WORKFLOWS })
    await ensureTemplate()
    const { db, url, schema, drop } = await throwawayDatabase()
    cleanups.push(drop)
    if (!(await db.ready())) throw new Error('the eval database did not migrate')
    const dir = await mkdtemp(path.join(os.tmpdir(), 'scadbuddy-eval-'))
    cleanups.push(() => rm(dir, { recursive: true, force: true }))
    const kek = kekFromBase64(randomBytes(32).toString('base64'))
    const keys = new PgPayloadKeys(db.sql, kek)
    // One input-hash key for both processes, as main.ts derives it from the one KEK.
    const hashKey = approvalHashKey(kek)
    const dataConverter = subjectDataConverter(keys, new FilePayloadStore(path.join(dir, 'payloads')))
    const client = new Client({ connection: env.connection, namespace: 'default', dataConverter })

    // What a session's turn reads: the model and the budget, in Settings.
    const stored = new SettingsStore(db.sql)
    if (options.model) await stored.set(SETTING_MODEL, options.model)
    await stored.set(SETTING_SESSION_BUDGET_USD, options.maxBudgetUsd ?? 0.5)

    // Each run's backend, and the message stream of the Claude Code its session spawned.
    let current: ScenarioBackend = new EvalBackend()
    let sink: Sink = { messages: [], decisions: [] }
    const services = {
      backend: createBackendClient(EVAL_BACKEND_URL, (input, init) => current.fetch(input, init)),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: options.renderWaitMs ?? 5_000,
    } satisfies ToolServices

    const tag = randomUUID().slice(0, 8)
    const workerHost = sessionHostQueue(`worker-${tag}`)

    async function agentProcess(role: 'api' | 'worker'): Promise<AgentProcess> {
      const parts = roleParts(role)
      const identity = `${role}-${tag}`
      const sessionHost = sessionHostQueue(identity)
      const stateDir = path.join(dir, role)
      await ensureStateDirs({ stateDir })
      const pool: Database = connectDatabase(url, { searchPath: schema })
      const listener = new PgEventListener(url, { searchPath: schema, retryMinMs: 20, log: () => {} })
      const settings = new SettingsStore(pool.sql)
      const audit = new AuditLog({ sql: pool.sql, settings: () => settings, hashKey })
      const p: AgentProcess = { spawned: 0 } as AgentProcess
      const run: QueryRunner = (spawn) => {
        p.spawned++
        const into = sink
        return tapped(
          runHarness({
            ...spawn,
            // The tier seam's verdict on each call, as it ran (harness/permissions.ts).
            onDecision: (toolName, decision) => {
              spawn.onDecision?.(toolName, decision)
              into.decisions.push({ toolName, decision: decision.decision, tier: decision.tier })
            },
            ...(options.stderr ? { stderr: options.stderr } : {}),
          }),
          (m) => into.messages.push(m),
        )
      }
      p.m = new SessionManager({
        sql: pool.sql,
        paths: { stateDir },
        credentials: fixedCredentials(options.credential),
        settings,
        payloadKeys: keys,
        approvalHashKey: hashKey,
        audit,
        // ScadBuddy's tools and their tiers, and its own plugin, as main.ts gives every session.
        ...harnessTools(services),
        ownPlugin: OWN_PLUGIN_DIR,
        run,
        newSessions: { max: 10_000, windowMs: 1 },
      })
      // Every session is an AgentSession: ready while the worker's session host polls.
      p.m.durableTurns = new DurableTurns({ client, sql: pool.sql, events: p.m.events, audit, liveHosts: async () => new Set([workerHost]) })
      p.m.durable = new DurableGate(client, { timeoutMs: 15_000 })
      listener.start()

      const shared: AgentWorker[] = []
      let hosts: HostWorkers | undefined
      let turnHost: TurnHost | undefined
      if (parts.sessionHost && parts.sharedWorkers) {
        turnHost = new TurnHost({
          core: p.m.turnCore,
          sql: pool.sql,
          images: new SessionBlobs(pool.sql),
          hostQueue: sessionHost,
          listen: (channel, onNotify) => listener.listenAlso(channel, onNotify),
          questions: p.m.questions,
        })
        const turnDeps: TurnToolsDeps = {
          sessions: new PgSessionOwners(pool.sql),
          approvals: new PgApprovalRecords(pool.sql),
          audit,
          servers: async () => ({ servers: [], problems: [], secrets: [], release: () => {} }),
        }
        const acquire = acquireActivities({ warm: p.m.warm, hostQueue: sessionHost, identity, liveHosts: async () => new Set([sessionHost]) })
        const common = { address: env.address, namespace: 'default', identity: workerIdentity(identity), dataConverter, shutdownGraceMs: 1_000, log: () => {} }
        shared.push(
          AgentWorker.start({
            ...common,
            taskQueue: TASK_QUEUE,
            activities: {
              ...gateActivities({ audit, sessions: new PgSessionOwners(pool.sql), tools: ALL_TOOLS, services }),
              ...sessionGateActivities(pool.sql),
              ...sessionFinishActivities(pool.sql, p.m.events),
              ...acquire,
            },
            workflows: { workflowBundle: bundle },
          }),
          AgentWorker.start({
            ...common,
            taskQueue: SESSION_QUEUE,
            activities: { [SESSION_SETTINGS_ACTIVITY]: () => readRuntimeSettings(settings) },
            workflows: { workflowBundle: bundle },
          }),
        )
        hosts = startHostWorkers({
          address: env.address,
          namespace: 'default',
          identity,
          dataConverter,
          // A pinned turn tool's calls (#2320), as main.ts's host worker serves them: the eval's turns offer none.
          pinned: { ...turnToolActivities(turnDeps), default: turnToolActivity(turnDeps) },
          sessions: {
            activities: { ...sessionTurnActivity(turnHost, { client }), ...releaseActivities(p.m.warm), ...acquire },
            slots: 10,
            shutdownGraceMs: 2_000,
            shutdownForceMs: 3_000,
          },
          shutdownGraceMs: 1_000,
        })
        await Promise.all([...shared.map((w) => w.running(60_000)), hosts.pinned.running(60_000), hosts.sessions?.running(60_000)])
        // Its /readyz (runtime/health.ts): Temporal lists it on every queue it serves.
        const queues = [
          { name: SESSION_QUEUE, type: 1 as const },
          { name: TASK_QUEUE, type: 2 as const },
          { name: sessionHost, type: 2 as const },
        ]
        const deadline = Date.now() + 60_000
        for (;;) {
          const missing = await queuesPolled(client, queues, workerIdentity(identity))
          if (missing === undefined) break
          if (Date.now() > deadline) throw new Error(`the eval worker never polled: ${missing}`)
          await new Promise((r) => setTimeout(r, 100))
        }
      }
      p.stop = async () => {
        const hosted = turnHost?.stop({ graceMs: 1_000, abortWaitMs: 1_000 })
        await Promise.all([...shared.map((w) => w.stop()), hosts?.stop(), p.m.stopTurns({ graceMs: 1_000, abortWaitMs: 1_000, ...(hosted ? { others: hosted } : {}) })])
        await listener.close()
        await pool.close()
      }
      cleanups.push(() => p.stop())
      return p
    }

    const worker = await agentProcess('worker')
    const api = await agentProcess('api')
    const started: string[] = []
    cleanups.push(async () => {
      for (const id of started)
        await client.workflow
          .getHandle(sessionWorkflowId(id))
          .terminate('eval over')
          .catch((err: unknown) => {
            if (!(err instanceof WorkflowNotFoundError)) throw err
          })
    })

    /** Ends a session's turn: interrupted as the panel's stop does, then its workflow terminated. */
    async function endSession(id: string, turn: Promise<unknown> | undefined): Promise<void> {
      await api.m.interrupt(id, EVAL_OWNER).catch(() => false)
      await Promise.race([turn?.catch(() => {}), new Promise((r) => setTimeout(r, 10_000))])
      await client.workflow
        .getHandle(sessionWorkflowId(id))
        .terminate('the eval run is over')
        .catch((err: unknown) => {
          if (!(err instanceof WorkflowNotFoundError)) throw err
        })
    }

    return {
      close,
      spawned: () => ({ api: api.spawned, worker: worker.spawned }),
      async workflow(sessionId) {
        const described = await client.workflow.getHandle(sessionWorkflowId(sessionId)).describe()
        return { type: described.type, taskQueue: described.taskQueue, status: described.status.name }
      },
      async run(scenario, runOptions = {}, backend = new EvalBackend()) {
        current = backend
        const seen: Sink = { messages: [], decisions: [] }
        sink = seen
        const { prompt, script } = scenario.prepare(backend)
        const startedAt = Date.now()
        const deadline = startedAt + (runOptions.timeoutMs ?? 300_000)
        const parked: { requestId: string; toolName: string; toolUseId: string }[] = []
        let error: string | undefined
        let sessionId: string | undefined
        let pending: Promise<unknown> | undefined
        try {
          const { session, turn } = await api.m.start(EVAL_OWNER, { origin: 'mcp', title: scenario.id, prompt })
          pending = turn?.done
          sessionId = session.id
          started.push(session.id)
          if (!turn) throw new Error('the session started no turn')
          let done = false
          const ended = () => (done = true)
          turn.done.then(ended, ended)
          const answered = new Set<string>()
          const polled: Polled = {}
          while (!done) {
            if (Date.now() > deadline) throw new Error(timedOut(polled))
            for (const entry of await pollPending(api.m.durable!, session.id, polled)) {
              if (entry.kind !== 'approval' || answered.has(entry.id)) continue
              answered.add(entry.id)
              const id = parseRequestId(entry.id)
              parked.push({ requestId: entry.id, toolName: entry.tool, toolUseId: id?.store === 'durable' ? id.toolUseId : '' })
              await respond(api.m, EVAL_OWNER, entry.id, { kind: 'approval', decision: 'deny' })
            }
            await Promise.race([turn.done, new Promise((r) => setTimeout(r, 200))])
          }
          const outcome = await turn.done
          if (outcome.kind !== 'result') error = outcome.kind === 'failed' ? `the turn failed: ${outcome.message}` : `the turn ended ${outcome.kind}`
        } catch (err) {
          error = err instanceof Error ? err.message : String(err)
          // A turn still running past the deadline would go on spending its budget, and its later
          // calls would reach the next run's backend: interrupt it, and end its workflow.
          if (sessionId) await endSession(sessionId, pending)
        }
        const folded = foldMessages(seen.messages)
        // Each parked call as the model made it: its name and its exact input.
        const approvals: Outcome['approvals'] = parked.map((p) => {
          const call = folded.toolCalls.find((c) => c.id === p.toolUseId)
          return { toolName: call?.name ?? p.toolName, input: call?.input ?? {}, toolUseId: p.toolUseId, tier: call?.tier ?? 'outward' }
        })
        const renders = await collectRenders(backend, collectWithin(runOptions, Date.now() - startedAt))
        return {
          scenario: scenario.id,
          ...folded,
          approvals,
          decisions: seen.decisions,
          backend,
          renders,
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
