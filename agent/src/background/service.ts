import { type BackgroundTask, type BackgroundTaskStore, type NewTask } from './store.js'

// A session's background tasks as the tools and the panel use them: a task is a row
// (store.ts) and a Temporal workflow `bgtask-<session id>-<id>` that fires it
// (temporal/backgroundTask.ts). Creating starts the workflow; cancelling cancels it,
// and the workflow ends the row.

export const BACKGROUND_WORKFLOW = 'BackgroundTask'
/** Names the session, so the payload codec seals the task's payloads under its key. */
export const backgroundWorkflowId = (sessionId: string, taskId: string) => `bgtask-${sessionId}-${taskId}`

/** What runs the tasks: Temporal in the service (temporal.ts), a stand-in in tests. */
export type TaskRunner = {
  start(task: Pick<BackgroundTask, 'id' | 'kind' | 'sessionId'>): Promise<void>
  /** Cancels a task's workflow; one that is not running is left as it is. */
  cancel(task: Pick<BackgroundTask, 'id' | 'sessionId'>): Promise<void>
  /** Ends a task's workflow at once, running nothing more (the session is being forgotten). */
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
    try {
      await this.#runner.start(row)
    } catch (err) {
      await this.store.finish(row.id, 'failed', `it could not be started: ${(err as Error).message}`)
      throw err
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
