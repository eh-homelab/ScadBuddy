import type { Client } from '@temporalio/client'
import { Hono } from 'hono'
import { SERVICE_HEADER } from '../app.js'
import { seenAtMs } from '../sessions/durable.js'
import { HOST_FRESH_MS } from '../temporal/session/affinity.js'
import type { TemporalHealth } from '../temporal/worker.js'

// The HTTP server of a worker-only agent process (SCADBUDDY_AGENT_ROLE=worker, plan Step 6.1):
// `/healthz` and `/readyz` on the agent's port, and nothing else, so `/api/v1/ai/*`, `/mcp` and
// the loopback `/internal/*` routes answer 404 here. Liveness, as the agent's own /healthz, is
// always 200 and reports; readiness waits until Temporal lists this process as a poller of every
// queue it serves, as the print worker's /readyz waits for its queue to route to it.

export type WorkerHealth = {
  status: 'ok'
  role: 'worker'
  database: 'ok' | 'unreachable' | 'not configured'
  secret_key: 'ok' | 'not configured'
  /** The worst of `queues`: `ok` only when every worker is. */
  temporal: TemporalHealth
  /** Each worker's state, by the queue it polls. */
  queues: Record<string, TemporalHealth>
}

export type WorkerHealthDeps = {
  database?: { ping(): Promise<boolean> }
  kek: { ok: boolean }
  workers: () => Record<string, TemporalHealth>
  /** Why this process is not ready yet, or undefined once it is (`queuesPolled`). */
  ready: () => Promise<string | undefined>
}

const WORST: readonly TemporalHealth[] = ['unavailable', 'connecting', 'ok']

export function workerHealthApp(deps: WorkerHealthDeps): Hono {
  const app = new Hono()
  app.use('*', async (c, next) => {
    await next()
    c.res.headers.set(SERVICE_HEADER, 'agent-worker')
  })
  app.get('/healthz', async (c) => {
    const dbOk = deps.database ? await deps.database.ping() : undefined
    const queues = deps.workers()
    const states = Object.values(queues)
    const body: WorkerHealth = {
      status: 'ok',
      role: 'worker',
      database: dbOk === undefined ? 'not configured' : dbOk ? 'ok' : 'unreachable',
      secret_key: deps.kek.ok ? 'ok' : 'not configured',
      temporal: WORST.find((s) => states.includes(s)) ?? 'unavailable',
      queues,
    }
    return c.json(body)
  })
  app.get('/readyz', async (c) => {
    const reason = await deps.ready()
    return reason === undefined ? c.json({ ready: true }) : c.json({ ready: false, reason }, 503)
  })
  return app
}

/**
 * temporal.api.enums.v1.TaskQueueType: WORKFLOW 1, ACTIVITY 2. `started: false`: this process
 * never started its worker on the queue (a missing database, KEK or Temporal; the start log says).
 */
export type PolledQueue = { name: string; type: 1 | 2; started?: boolean }
const DESCRIBE_DEADLINE_MS = 3_000

/**
 * Why `identity` is not yet a fresh poller of every one of `queues`, or undefined once it is. A
 * Temporal that does not answer is a reason too: readiness fails closed.
 */
export async function queuesPolled(
  client: Pick<Client, 'workflowService' | 'options' | 'withDeadline'>,
  queues: readonly PolledQueue[],
  identity: string,
): Promise<string | undefined> {
  const unstarted = queues.find((q) => q.started === false)
  if (unstarted) return `this process started no worker on "${unstarted.name}"; its start log says why`
  try {
    for (const queue of queues) {
      // Each call its own deadline and clock: a slow first answer leaves the rest their full time.
      const asked = Date.now()
      const { pollers } = await client.withDeadline(asked + DESCRIBE_DEADLINE_MS, () =>
        client.workflowService.describeTaskQueue({
          namespace: client.options.namespace,
          taskQueue: { name: queue.name },
          taskQueueType: queue.type,
        }),
      )
      if (!(pollers ?? []).some((p) => p.identity === identity && Date.now() - seenAtMs(p.lastAccessTime) < HOST_FRESH_MS)) {
        return `this worker does not poll Temporal's "${queue.name}" queue yet`
      }
    }
  } catch {
    return 'Temporal did not answer'
  }
  return undefined
}
