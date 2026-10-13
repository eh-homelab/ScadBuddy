import type { Context, Hono } from 'hono'
import type { AuditRepo } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { BackgroundTasks } from '../background/service.js'
import type { BackgroundTask } from '../background/store.js'
import { describeCron } from '../background/schedule.js'
import type { OriginPolicy } from '../http/origins.js'
import { type SessionManager, SessionError } from '../sessions/manager.js'
import { sameOwner } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The assistant panel's background tasks: what a session's CronCreate and
// ScheduleWakeup left running (src/background/), for the browser user.
//
//   GET    /api/v1/ai/sessions/:id/background-tasks              { tasks: [...] }, the active ones
//                                                                 first, then the last 20 ended
//   DELETE /api/v1/ai/sessions/:id/background-tasks/:taskId      → 200 {task}; 404 when it is not
//                                                                 an active task of the session
//
// Access is the session routes' (as routes/sessionFiles.ts): reads pass `uiReadProblem`
// and need a session the browser user may see; a cancel passes `uiRequestProblem` and
// only the session's owner may make it. A cancel is a `resource` audit row.

export type BackgroundTaskView = {
  id: string
  kind: BackgroundTask['kind']
  description: string
  /** The schedule as words: "Every 5 minutes", or the wakeup's delay. */
  schedule: string
  prompt: string
  state: BackgroundTask['state']
  next_at: string | null
  fired: number
  last_fired_at: string | null
  expires_at: string | null
  detail: string | null
  created_at: string
}

export function taskView(t: BackgroundTask): BackgroundTaskView {
  const schedule =
    t.kind === 'cron'
      ? `${describeCron(String(t.spec.cron))}${t.spec.recurring === false ? ', once' : ''}`
      : t.kind === 'wakeup'
        ? `once, ${String(t.spec.delaySeconds)} s after it was set`
        : t.kind === 'monitor'
          ? 'monitor'
          : t.kind === 'workflow'
            ? `workflow ${String(t.spec.name)}`
            : t.kind
  return {
    id: t.id,
    kind: t.kind,
    description: t.description,
    schedule,
    // A monitor's "prompt" is what it watches: its command, or its socket's URL; a workflow's, its description.
    prompt:
      typeof t.spec.prompt === 'string'
        ? t.spec.prompt
        : t.kind === 'workflow'
          ? String(t.spec.description ?? '')
        : typeof t.spec.command === 'string'
          ? t.spec.command
          : String((t.spec.ws as { url?: string } | undefined)?.url ?? ''),
    state: t.state,
    next_at: t.nextAt?.toISOString() ?? null,
    fired: t.fired,
    last_fired_at: t.lastFiredAt?.toISOString() ?? null,
    expires_at: t.expiresAt?.toISOString() ?? null,
    detail: t.detail,
    created_at: t.createdAt.toISOString(),
  }
}

declare module '../app.js' {
  interface AppDeps {
    /** A session's background tasks (src/background/); undefined without Temporal. */
    backgroundTasks?: BackgroundTasks | undefined
  }
}

const BASE = '/api/v1/ai/sessions'
/** How many ended tasks the list shows after the active ones. */
const ENDED_SHOWN = 20

export function registerBackgroundTaskRoutes(
  app: Hono,
  deps: {
    sessions: SessionManager | undefined
    tasks: BackgroundTasks | undefined
    ready: () => Promise<boolean>
    remoteAddress: RemoteAddress
    origins: OriginPolicy
    audit?: AuditRepo | undefined
  },
): void {
  async function available(c: Context): Promise<{ sessions: SessionManager; tasks: BackgroundTasks } | Response> {
    if (!deps.sessions || !deps.tasks) return c.json({ detail: 'background tasks need the database and Temporal' }, 503)
    if (!(await deps.ready())) return c.json({ detail: 'the AI database is unreachable or its migrations have not applied; see /healthz' }, 503)
    return { sessions: deps.sessions, tasks: deps.tasks }
  }

  app.get(`${BASE}/:id/background-tasks`, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'background task reads')
    if (problem) return c.json({ detail: problem }, 403)
    const got = await available(c)
    if (got instanceof Response) return got
    try {
      const session = await got.sessions.get(c.req.param('id'), BROWSER_USER)
      const all = await got.tasks.store.list(session.id, { all: true })
      const active = all.filter((t) => t.state === 'active')
      const ended = all.filter((t) => t.state !== 'active').slice(0, ENDED_SHOWN)
      return c.json({ tasks: [...active, ...ended].map(taskView) })
    } catch (err) {
      if (err instanceof SessionError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  })

  app.delete(`${BASE}/:id/background-tasks/:taskId`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'background task changes')
    if (problem) return c.json({ detail: problem }, 403)
    const got = await available(c)
    if (got instanceof Response) return got
    try {
      const session = await got.sessions.get(c.req.param('id'), BROWSER_USER)
      if (!sameOwner(BROWSER_USER, session.owner)) {
        throw new SessionError('forbidden', `session ${session.id} is controlled by ${session.owner.label}; take it over first`)
      }
      const taskId = c.req.param('taskId')
      const cancelled = await got.tasks.cancel(session.id, taskId, 'the user in the panel')
      await deps.audit?.record({
        kind: 'resource',
        action: 'background_task.cancel',
        surface: 'http',
        actor: UI_ACTOR,
        outcome: cancelled ? 'ok' : 'refused',
        clientIp: deps.remoteAddress(c),
        sessionId: session.id,
        inputSummary: taskId,
      })
      if (!cancelled) return c.json({ detail: `no active background task ${taskId} in this session` }, 404)
      return c.json({ task: taskView(cancelled) })
    } catch (err) {
      if (err instanceof SessionError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  })
}

export const route: RouteModule = {
  register(app, deps) {
    registerBackgroundTaskRoutes(app, {
      sessions: deps.sessions,
      tasks: deps.backgroundTasks,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      audit: deps.audit,
    })
  },
}
