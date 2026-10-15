import type { Hono } from 'hono'
import type { FlowQueue } from '../flows/queue.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// GET /api/v1/ai/flow-runs/{run_id}/queue (#2482, spec 2026-10-11 §4.5): a flow run's
// one queue, its own parked calls, its child sessions' and its requested grant, with the
// context beside each (flows/queue.ts). The person's read, for the run page and the
// starter's assistant panel: behind guard.ts `uiReadProblem` like the pending-input
// reads. No agent tool reads it (tools/coverage.ts): a run's queue is the person's, and
// the starter session hears of the calls waiting for it in its escalation turn.

export const FLOW_QUEUE_PATH = '/api/v1/ai/flow-runs/:id/queue'

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

export type FlowQueueRouteDeps = {
  queue: Pick<FlowQueue, 'forRun'> | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export function registerFlowQueueRoutes(app: Hono, deps: FlowQueueRouteDeps): void {
  app.get(FLOW_QUEUE_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'flow run queue reads')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.queue) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    const queue = await deps.queue.forRun(c.req.param('id'))
    return queue ? c.json(queue) : c.json({ detail: `no flow run ${c.req.param('id')}` }, 404)
  })
}

export const route: RouteModule = {
  register(app, deps) {
    const sessions = deps.sessions
    registerFlowQueueRoutes(app, {
      // Read per request, never at registration: an app is built before its manager is used.
      queue: sessions ? { forRun: (id) => sessions.queue.forRun(id) } : undefined,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
