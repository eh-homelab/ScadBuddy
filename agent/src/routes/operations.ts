import type { Context } from 'hono'
import type { CommandOutcome, Commands } from '../operations/run.js'
import { uiReadProblem } from './guard.js'
import type { RouteModule } from './module.js'

// GET /api/v1/ai/operations/{id} (spec 2026-10-01 §4.2 "Our record", #1055): one of the
// agent's commands, recorded in ai_operations, which a client follows after a 202 until
// it is not `running`. The backend's GET /api/v1/operations/{id} for the agent's own.

export const NO_COMMANDS =
  "the agent's commands run on Temporal: SCADBUDDY_TEMPORAL_ADDRESS and SCADBUDDY_DATABASE_URL must both be set"

/** A command's outcome as the route answers it: the body with `status`, a 202, or the problem. */
export function commandResponse(c: Context, outcome: CommandOutcome, status: 200 | 201) {
  if (outcome.status === 'done') return c.json(outcome.result ?? null, status)
  if (outcome.status === 'running') return c.json(outcome.operation, 202)
  const { problem, retryAfter } = outcome
  if (retryAfter !== undefined) c.header('Retry-After', String(retryAfter))
  // The agent's error bodies are `{detail}` plus what the route adds (`problems`).
  return c.json(
    { detail: problem.detail, ...(problem.type ? { type: problem.type } : {}), ...problem.extensions },
    problem.status as 400,
  )
}

declare module '../app.js' {
  interface AppDeps {
    /** The agent's commands (#1055, operations/run.ts); undefined without Temporal or a database. */
    commands?: Commands | undefined
  }
}

export const route: RouteModule = {
  register(app, deps) {
    app.get('/api/v1/ai/operations/:id', async (c) => {
      const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'operation reads')
      if (problem) return c.json({ detail: problem }, 403)
      if (!deps.commands) return c.json({ detail: NO_COMMANDS }, 503)
      const op = await deps.commands.get(c.req.param('id'))
      if (!op) return c.json({ detail: `no operation ${c.req.param('id')}` }, 404)
      return c.json(op)
    })
  },
}
