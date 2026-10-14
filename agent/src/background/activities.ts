import { SessionError, type SessionManager } from '../sessions/manager.js'
import { nextFire } from './schedule.js'
import type { BackgroundTask, BackgroundTaskStore } from './store.js'

// The activities of a background task's workflow (temporal/backgroundTask.ts), on the
// agent's `agent-tools` queue. The workflow carries only the task's id: the prompt,
// the schedule and who made it are read from the row each time, so nothing a user
// wrote is kept in Temporal's history, and a cancel (which ends the row first) stops
// the next fire wherever the workflow is.

export const BG_NEXT = 'bg_next'
export const BG_FIRE = 'bg_fire'
export const BG_FINISH = 'bg_finish'

/** How long a fire waits for a busy session before it gives that occurrence up. */
export const BUSY_GIVE_UP_MS = 60 * 60 * 1000

export type NextStep = { at: number } | { end: string }
export type FireResult =
  | { result: 'sent' }
  | { result: 'busy' }
  /** A cron occurrence given up after BUSY_GIVE_UP_MS; the next one is still due. */
  | { result: 'skipped' }
  | { result: 'end'; state: 'done' | 'failed'; reason: string }

/** Sends a task's prompt as the session's next message, as the one who made the task. */
export type TaskSender = (task: BackgroundTask, prompt: string, context: string) => Promise<FireResult>

/** A sender over the session manager: a busy session is asked again; any other refusal ends the task. */
export function managerSender(manager: Pick<SessionManager, 'send'>): TaskSender {
  return async (task, prompt, context) => {
    try {
      await manager.send(task.sessionId, task.createdBy, prompt, { context })
      return { result: 'sent' }
    } catch (err) {
      if (!(err instanceof SessionError)) throw err
      if (err.code === 'busy' || err.code === 'rate_limited' || err.code === 'unavailable') return { result: 'busy' }
      return { result: 'end', state: 'failed', reason: `not sent: ${err.message}` }
    }
  }
}

export type BackgroundActivityDeps = {
  store: BackgroundTaskStore
  send: TaskSender
  tz: string
  now?: () => Date
}

const isCron = (t: BackgroundTask) => t.kind === 'cron'
const recurring = (t: BackgroundTask) => isCron(t) && t.spec.recurring !== false

/** What the session's model reads beside the prompt, so it knows the turn was not typed now. */
export function fireContext(t: BackgroundTask): string {
  if (t.kind === 'wakeup') return `(This message is the ScheduleWakeup you set; its reason was: ${String(t.spec.reason ?? 'none given')}.)`
  return `(This message is ${recurring(t) ? 'recurring' : 'one-shot'} job ${t.id} you scheduled with CronCreate, "${String(t.spec.cron)}"; CronDelete ${t.id} stops it.)`
}

export function backgroundActivities(deps: BackgroundActivityDeps) {
  const now = deps.now ?? (() => new Date())
  return {
    async [BG_NEXT](input: { taskId: string }): Promise<NextStep> {
      const t = await deps.store.get(input.taskId)
      if (!t || t.state !== 'active') return { end: 'gone' }
      let at: Date | undefined
      if (t.kind === 'wakeup') {
        if (t.fired > 0) return { end: 'fired' }
        at = new Date(String(t.spec.at))
      } else if (isCron(t)) {
        if (!recurring(t) && t.fired > 0) return { end: 'fired' }
        at = nextFire(String(t.spec.cron), now(), String(t.spec.tz ?? deps.tz))
        if (!at) return { end: 'its schedule never fires again' }
        if (t.expiresAt && at > t.expiresAt) return { end: 'expired after 7 days' }
      } else {
        return { end: `a ${t.kind} task is not fired on a schedule` }
      }
      await deps.store.scheduled(t.id, at)
      return { at: at.getTime() }
    },

    /** One fire; `waitedMs` is how long this occurrence has waited for an idle session. */
    async [BG_FIRE](input: { taskId: string; waitedMs: number }): Promise<FireResult> {
      const t = await deps.store.get(input.taskId)
      if (!t || t.state !== 'active') return { result: 'end', state: 'done', reason: 'gone' }
      const sent = await deps.send(t, String(t.spec.prompt), fireContext(t))
      if (sent.result === 'sent') {
        await deps.store.fired(t.id, now())
        return sent
      }
      if (sent.result === 'busy' && input.waitedMs >= BUSY_GIVE_UP_MS) {
        if (recurring(t)) return { result: 'skipped' }
        return { result: 'end', state: 'done', reason: 'not fired: the session was busy for an hour' }
      }
      return sent
    },

    async [BG_FINISH](input: { taskId: string; state: 'done' | 'cancelled' | 'failed'; detail?: string }): Promise<void> {
      await deps.store.finish(input.taskId, input.state, input.detail)
    },
  }
}
