import { randomBytes, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { Client, Connection } from '@temporalio/client'
import { bundleWorkflowCode, NativeConnection } from '@temporalio/worker'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { context as otelContext, trace } from '@opentelemetry/api'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { EAGER_ATTRIBUTE, toolCallStarter } from '../src/tools/activityProjection.js'
import { PgPayloadKeys, subjectDataConverter } from '../src/temporal/payloadCodec.js'
import { toolCallWorkflowId } from '../src/temporal/toolCall.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { until } from './support/until.js'
import { testTracing } from './support/tracing.js'

/** Runs `fn` under a span of its own, as a turn's call is (telemetry/turn.ts), and says what the starter set on it as `scadbuddy.eager`. */
async function eagerOf(fn: () => Promise<unknown>): Promise<unknown> {
  testTracing()
  const span = trace.getTracer('test').startSpan('call')
  try {
    await otelContext.with(trace.setSpan(otelContext.active(), span), fn)
  } finally {
    span.end()
  }
  return (span as unknown as { attributes: Record<string, unknown> }).attributes[EAGER_ATTRIBUTE]
}

// What Temporal adds around a ToolCall's activity (#5865), on a dev server and a real AgentWorker
// as main.ts runs it, with calls started by toolCallStarter as a turn starts them: N calls of a
// trivial activity, each timed from its history (schedule-to-start and run of its two workflow
// tasks and of its activity) and from the starter (start to result). The prod case is the
// payload codec (PgPayloadKeys over Postgres) on a versioned worker (a Deployment version,
// AUTO_UPGRADE), with and without eager start (ToolCallStarterDeps `eager`).
//
// The timed cases run only with TOOL_CALL_BENCH_REPORT=1 or TOOL_CALL_BENCH_ASSERT=1; the tests of
// how a call starts (and on a superseded build) run always.
// Knobs: TOOL_CALL_BENCH_N (default 10) calls a case; TOOL_CALL_BENCH_MATRIX=1 adds the cases
// without the codec or versioning; TOOL_CALL_BENCH_REPORT=1 prints each case's phases;
// TOOL_CALL_BENCH_ASSERT=1 fails a case whose median overhead passes OVERHEAD_LIMIT_MS (off by
// default, so a slow shared runner cannot fail a correct change; the eager start's span test runs always);
// TOOL_CALL_BENCH_ADDRESS runs on a server of the caller's own (one with a CPU limit, say).

const N = Number(process.env.TOOL_CALL_BENCH_N ?? 10)
const REPORT = process.env.TOOL_CALL_BENCH_REPORT === '1'
const ASSERT = process.env.TOOL_CALL_BENCH_ASSERT === '1'
/**
 * The most a call's median overhead may be here, with TOOL_CALL_BENCH_ASSERT=1. Prod's target for a healthy worker is 300 ms
 * (#5865); a dev server on a loaded machine measured medians up to ~520 ms (codec and versioning,
 * no eager start, load average ~89 on 22 cores, 2026-11-09), so this catches prod's 3.4 s class of
 * regression; it is off by default because a shared runner may still be slower than that.
 */
const OVERHEAD_LIMIT_MS = 1000
const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))
const FAILURE_CONVERTER = fileURLToPath(new URL('../src/temporal/failureConverter.ts', import.meta.url))

type Phases = {
  wft1Wait: number
  wft1Run: number
  actWait: number
  actRun: number
  wft2Wait: number
  wft2Run: number
  /** From the starter's call to its answer, as the turn waits. */
  call: number
}

const ms = (t: { seconds?: unknown; nanos?: number | null } | null | undefined): number => Number(t?.seconds ?? 0) * 1000 + (t?.nanos ?? 0) / 1e6

/** The phases of a closed ToolCall, from its history's event times. */
async function phasesOf(client: Client, workflowId: string, callMs: number): Promise<Phases> {
  const events = (await client.workflow.getHandle(workflowId).fetchHistory()).events ?? []
  const at = (pred: (e: (typeof events)[number]) => unknown, nth = 0): number => ms(events.filter(pred)[nth]?.eventTime)
  const wft = (kind: 'Scheduled' | 'Started' | 'Completed', nth: number) =>
    at((e) => e[`workflowTask${kind}EventAttributes` as 'workflowTaskScheduledEventAttributes'], nth)
  return {
    wft1Wait: wft('Started', 0) - wft('Scheduled', 0),
    wft1Run: wft('Completed', 0) - wft('Started', 0),
    actWait: at((e) => e.activityTaskStartedEventAttributes) - at((e) => e.activityTaskScheduledEventAttributes),
    actRun: at((e) => e.activityTaskCompletedEventAttributes) - at((e) => e.activityTaskStartedEventAttributes),
    wft2Wait: wft('Started', 1) - wft('Scheduled', 1),
    wft2Run: wft('Completed', 1) - wft('Started', 1),
    call: callMs,
  }
}

const quantile = (xs: number[], q: number): number => {
  const s = [...xs].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor(s.length * q))] ?? 0
}

/**
 * What Temporal does with an eager start on a superseded build (measured below; CLI 1.9.1, Server
 * 1.32.0, 2026-11-10): it grants it, and the old build's worker runs the first task and the activity.
 */
const SUPERSEDED = { how: 'started', ran: ['old'] }

type Case = { codec: boolean; versioned: boolean; eager: boolean }
const CASES: Case[] = [
  { codec: true, versioned: true, eager: false },
  { codec: true, versioned: true, eager: true },
  ...(process.env.TOOL_CALL_BENCH_MATRIX === '1'
    ? [
        { codec: false, versioned: false, eager: false },
        { codec: true, versioned: false, eager: false },
        { codec: false, versioned: true, eager: false },
      ]
    : []),
]

describe.skipIf(!TEMPORAL_CLI || !TEST_DATABASE_URL)(
  `ToolCall overhead around its activity (#5865)${TEMPORAL_SKIP}${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let server: { address: string; namespace: string; teardown: () => Promise<void> }
    let code: string
    let db: Database
    let drop: () => Promise<void>

    beforeAll(async () => {
      const address = process.env.TOOL_CALL_BENCH_ADDRESS
      if (address) server = { address, namespace: 'default', teardown: async () => {} }
      else {
        const env = await localTemporal()
        server = { address: env.address, namespace: env.namespace ?? 'default', teardown: () => env.teardown() }
      }
      // As scripts/bundle-workflows.mjs builds prod's bundle.
      ;({ code } = await bundleWorkflowCode({ workflowsPath: WORKFLOWS, failureConverterPath: FAILURE_CONVERTER }))
      ;({ db, drop } = await throwawayDatabase())
    }, 180_000)
    afterAll(async () => {
      await drop?.()
      await server?.teardown()
    })

    // Only when asked for: the span test below checks how calls start on every run.
    it.skipIf(!REPORT && !ASSERT).each(CASES)('codec $codec, versioned $versioned, eager $eager', async ({ codec, versioned, eager }) => {
      const queue = `bench-${randomUUID().slice(0, 8)}`
      const keys = codec ? new PgPayloadKeys(db.sql, { id: 'bench', key: randomBytes(32) }) : undefined
      const converter = keys ? subjectDataConverter(keys) : undefined
      const worker = AgentWorker.start({
        address: server.address,
        namespace: server.namespace,
        taskQueue: queue,
        activities: { 'tool:bench': async () => ({ ok: true, content: [{ type: 'text', text: 'ok' }] }) },
        workflows: { workflowBundle: { code } },
        ...(converter ? { dataConverter: { ...converter, failureConverterPath: FAILURE_CONVERTER } } : {}),
        ...(versioned ? { deployment: { name: `bench-${queue}`, buildId: 'b1', queues: () => [queue] } } : {}),
        retryMs: 200,
        log: () => {},
      })
      const connection = await Connection.connect({ address: server.address })
      const client = new Client({ connection, namespace: server.namespace, ...(converter ? { dataConverter: converter } : {}) })
      try {
        await worker.running()
        await until(() => worker.current(), 30_000)
        const eagerClient = new Client({ connection: worker.connection()!, namespace: server.namespace, ...(converter ? { dataConverter: converter } : {}) })
        const start = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue, timeoutMs: 60_000, ...(eager ? { eager: () => eagerClient } : {}) })
        const session = randomUUID()
        const run = async (n: number): Promise<Phases> => {
          const toolUseId = `toolu_bench_${n}`
          const t0 = performance.now()
          const result = await start({ sessionId: session, toolUseId, tool: 'bench', input: { n }, signal: new AbortController().signal })
          const callMs = performance.now() - t0
          expect(result).toEqual({ content: [{ type: 'text', text: 'ok' }] })
          return phasesOf(client, toolCallWorkflowId(session, toolUseId), callMs)
        }
        // Warm: the first call makes the session's key and loads the workflow sandbox.
        await run(-1)
        const all: Phases[] = []
        for (let n = 0; n < N; n += 1) all.push(await run(n))
        const overhead = all.map((p) => p.call - p.actRun)
        const of = (k: keyof Phases) => all.map((p) => p[k])
        if (REPORT) {
          const row = (name: string, xs: number[]) => `${name.padEnd(9)} median ${quantile(xs, 0.5).toFixed(1).padStart(7)} ms  p95 ${quantile(xs, 0.95).toFixed(1).padStart(7)} ms`
          const keysOf = Object.keys(all[0]!) as (keyof Phases)[]
          console.log([`codec ${codec}, versioned ${versioned}, eager ${eager}, N=${N}`, ...keysOf.map((k) => row(k, of(k))), row('overhead', overhead)].join('\n'))
        }
        if (ASSERT) expect(quantile(overhead, 0.5)).toBeLessThan(OVERHEAD_LIMIT_MS)
      } finally {
        await connection.close()
        await worker.stop()
      }
    }, 300_000)

    it("measures an eager start on a build a later one has superseded (a rollout's old replica)", async () => {
      const queue = `bench-${randomUUID().slice(0, 8)}`
      const deployment = `bench-${queue}`
      const common = { address: server.address, namespace: server.namespace, taskQueue: queue, workflows: { workflowBundle: { code } }, retryMs: 200, log: () => {} }
      const ran: string[] = []
      const answer = (build: string) => async () => {
        ran.push(build)
        return { ok: true, content: [{ type: 'text', text: 'ok' }] }
      }
      const old = AgentWorker.start({ ...common, identity: 'old-build', activities: { 'tool:bench': answer('old') }, deployment: { name: deployment, buildId: 'b1', queues: () => [queue] } })
      let next: AgentWorker | undefined
      const connection = await Connection.connect({ address: server.address })
      try {
        await old.running()
        await until(() => old.current(), 30_000)
        next = AgentWorker.start({ ...common, identity: 'new-build', activities: { 'tool:bench': answer('new') }, deployment: { name: deployment, buildId: 'b2', queues: () => [queue] } })
        await next.running()
        await until(() => next!.current(), 30_000)
        // The latch: the old build's worker still offers its connection.
        expect(old.connection()).toBeDefined()
        const client = new Client({ connection, namespace: server.namespace })
        const start = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue, timeoutMs: 60_000, eager: () => new Client({ connection: old.connection()!, namespace: server.namespace }) })
        const session = randomUUID()
        const how = await eagerOf(() => start({ sessionId: session, toolUseId: 'toolu_bench_old', tool: 'bench', input: {}, signal: new AbortController().signal }))
        if (REPORT) console.log(`superseded build: eager ${String(how)}, activity on ${ran.join(',')}`)
        expect({ how, ran }).toEqual(SUPERSEDED)
      } finally {
        await connection.close()
        await next?.stop()
        await old.stop()
      }
    }, 120_000)

    it("starts the call on the starter's client when the eager connection has failed, and the call's span says how it started", async () => {
      const queue = `bench-${randomUUID().slice(0, 8)}`
      const worker = AgentWorker.start({
        address: server.address,
        namespace: server.namespace,
        taskQueue: queue,
        activities: { 'tool:bench': async () => ({ ok: true, content: [{ type: 'text', text: 'ok' }] }) },
        workflows: { workflowBundle: { code } },
        retryMs: 200,
        log: () => {},
      })
      const connection = await Connection.connect({ address: server.address })
      const gone = await NativeConnection.connect({ address: server.address })
      await gone.close()
      try {
        await worker.running()
        const client = new Client({ connection, namespace: server.namespace })
        const start = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue, timeoutMs: 60_000, eager: () => new Client({ connection: gone, namespace: server.namespace }) })
        const session = randomUUID()
        let result: unknown
        expect(await eagerOf(async () => (result = await start({ sessionId: session, toolUseId: 'toolu_bench_gone', tool: 'bench', input: {}, signal: new AbortController().signal })))).toBe('failed')
        expect(result).toEqual({ content: [{ type: 'text', text: 'ok' }] })
        // And with no worker to start on (`eager` answers undefined), on the starter's client too.
        const plain = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue, timeoutMs: 60_000, eager: () => undefined })
        expect(await eagerOf(async () => expect(await plain({ sessionId: session, toolUseId: 'toolu_bench_none', tool: 'bench', input: {}, signal: new AbortController().signal })).toEqual({
          content: [{ type: 'text', text: 'ok' }],
        }))).toBe('fallback')
        // On the worker's own connection, the first task comes back in the start's answer.
        const eagerStart = toolCallStarter({ client, blobs: new SessionBlobs(db.sql), queue, timeoutMs: 60_000, eager: () => new Client({ connection: worker.connection()!, namespace: server.namespace }) })
        expect(await eagerOf(() => eagerStart({ sessionId: session, toolUseId: 'toolu_bench_eager', tool: 'bench', input: {}, signal: new AbortController().signal }))).toBe('started')
        // A re-send of the same call finds its execution (USE_EXISTING): no task comes back.
        expect(await eagerOf(() => eagerStart({ sessionId: session, toolUseId: 'toolu_bench_eager', tool: 'bench', input: {}, signal: new AbortController().signal }))).toBe('declined')
        // An unversioned worker's build is current at once: its connection is offered while it polls.
        expect(worker.connection()).toBeDefined()
      } finally {
        await connection.close()
        await worker.stop()
      }
      // Never once it has stopped.
      expect(worker.connection()).toBeUndefined()
    }, 120_000)
  },
)
