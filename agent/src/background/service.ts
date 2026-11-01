import { type BackgroundTask, type BackgroundTaskStore, type NewTask } from './store.js'

// A session's background tasks as the tools and the panel use them: a task is a row
// (store.ts) and what fires it on Temporal: a recurring cron job is a Schedule
// `bgtask-<session id>-<id>` whose every fire is a short `BackgroundFire` workflow
// (plan 2026-10-13 Step 9.8); any other task is a workflow `bgtask-<session id>-<id>`
// (temporal/backgroundTask.ts). Creating starts it; cancelling ends it, and the fire or the
// workflow ends the row.

export const BACKGROUND_WORKFLOW = 'BackgroundTask'
/** Names the session, so the payload codec seals the task's payloads under its key. */
export const backgroundWorkflowId = (sessionId: string, taskId: string) => `bgtask-${sessionId}-${taskId}`

/** One fire of a recurring cron job's Schedule. */
export const BACKGROUND_FIRE_WORKFLOW = 'BackgroundFire'
/**
 * The Schedule's action's workflow id; Temporal adds each fire's time (`-<RFC 3339>`), which
 * the fire reads its session and task from (temporal/backgroundTask.ts `fireTask`). It
 * carries no arguments, so nothing of the task is in a fire's input. Its payloads are left
 * unsealed on purpose (payloadCodec.ts `subjectOf` names no subject for it): they are the
 * task's id and the fire's outcome, never the prompt, which each activity reads from the row.
 */
export const backgroundFireId = (sessionId: string, taskId: string) => `bgfire-${sessionId}-${taskId}`

/** Whether a task is fired by a Schedule: a recurring cron job. */
export const isScheduled = (task: Pick<BackgroundTask, 'kind' | 'spec'>): boolean => task.kind === 'cron' && task.spec.recurring !== false

/** What a runner needs of a task to start it. */
export type StartedTask = Pick<BackgroundTask, 'id' | 'kind' | 'sessionId' | 'spec' | 'expiresAt'>

/** What runs the tasks: Temporal in the service (temporal.ts), a stand-in in tests. */
export type TaskRunner = {
  /** `end`: the task has nothing to run (a recurring job with no occurrence before its expiry), so its row ends `done` with that detail. */
  start(task: StartedTask): Promise<void | { end: string }>
  /** Cancels a task's workflow, or deletes its Schedule; one that is not running is left as it is. */
  cancel(task: Pick<BackgroundTask, 'id' | 'sessionId'>): Promise<void>
  /** Ends a task's workflow, or its Schedule and its running fire, at once, running nothing more (the session is being forgotten). */
  terminate(task: Pick<BackgroundTask, 'id' | 'sessionId'>, reason: string): Promise<void>
}

export class BackgroundTasks {
  readonly store: BackgroundTaskStore
  readonly #runner: TaskRunner
  constructor(deps: { store: BackgroundTaskStore; runner: TaskRunner }) {
    this.store = deps.store
    this.#runner = deps.runner
  }

  /** Records a task and starts its workflow; undefined when the session is at its cap. */
  async create(t: NewTask): Promise<BackgroundTask | undefined> {
    const row = await this.store.create(t)
    if (!row) return undefined
    let started: void | { end: string }
    try {
      started = await this.#runner.start(row)
    } catch (err) {
      await this.store.finish(row.id, 'failed', `it could not be started: ${(err as Error).message}`)
      throw err
    }
    if (started) {
      await this.store.finish(row.id, 'done', started.end)
      return { ...row, state: 'done', detail: started.end }
    }
    return row
  }

  /** One of a session's tasks, or undefined. */
  async get(sessionId: string, id: string): Promise<BackgroundTask | undefined> {
    const row = await this.store.get(id)
    return row && row.sessionId === sessionId ? row : undefined
  }

  /**
   * Cancels one of a session's active tasks: the row ends first, so it never fires
   * again whatever the workflow is doing (a fire reads the row), then the workflow is
   * cancelled. Undefined when the session has no such active task.
   */
  async cancel(sessionId: string, id: string, by: string): Promise<BackgroundTask | undefined> {
    const row = await this.get(sessionId, id)
    if (!row || row.state !== 'active') return undefined
    if (!(await this.store.finish(id, 'cancelled', `cancelled by ${by}`))) return undefined
    await this.#runner.cancel(row)
    return { ...row, state: 'cancelled', detail: `cancelled by ${by}` }
  }

  /**
   * Ends every active task of a session (archived, marked done, or forgotten): the rows
   * first, then the workflows, cancelled, or with `terminate` ended at once so none of
   * them encodes another payload under a key about to be deleted. How many ended.
   */
  async endSession(sessionId: string, why: string, options: { terminate?: boolean } = {}): Promise<number> {
    let n = 0
    for (const row of await this.store.list(sessionId)) {
      if (!(await this.store.finish(row.id, 'cancelled', why))) continue
      n += 1
      if (options.terminate) await this.#runner.terminate(row, why)
      else await this.#runner.cancel(row)
    }
    return n
  }
}
