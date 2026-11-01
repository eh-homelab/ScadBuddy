import { type Client, ScheduleAlreadyRunning, ScheduleNotFoundError, WorkflowNotFoundError } from '@temporalio/client'
import { agentTimezone, nextFire, scheduleCrons } from './schedule.js'
import {
  BACKGROUND_FIRE_WORKFLOW,
  BACKGROUND_WORKFLOW,
  backgroundFireId,
  backgroundWorkflowId,
  isScheduled,
  type StartedTask,
  type TaskRunner,
} from './service.js'
import type { BackgroundTask, BackgroundTaskStore } from './store.js'

// Background tasks on Temporal. A recurring cron job is a Schedule (plan 2026-10-13 Step 9.8,
// R6 of #4773): its cron in the task's timezone, ending at the task's expiry, each fire a
// short `BackgroundFire` workflow that sends the prompt once the session is idle. Any other
// task is one `BackgroundTask` workflow (temporal/backgroundTask.ts).

/**
 * How late a missed fire may still be taken (after a Temporal outage): one, as the timer
 * workflow fired once on waking, never a backlog.
 */
export const CRON_CATCHUP = '1 minute'

/**
 * Why a recurring cron job has no occurrence left before its expiry, or undefined while it
 * has one: a Schedule ends at the expiry and no fire runs after it, so such a job's row is
 * ended here, not by a fire (bg_next's rule).
 */
export function scheduleEnd(task: Pick<BackgroundTask, 'spec' | 'expiresAt'>, now = new Date()): string | undefined {
  const at = nextFire(String(task.spec.cron), now, String(task.spec.tz ?? agentTimezone()))
  if (!at) return 'its schedule never fires again'
  if (task.expiresAt && at > task.expiresAt) return 'expired after 7 days'
  return undefined
}

/** Creates a recurring cron job's Schedule; one already there is left as it is (idempotent). Whether it made one. */
export async function scheduleTask(client: Client, task: StartedTask, taskQueue: string, retryMs?: number): Promise<boolean> {
  try {
    await client.schedule.create({
      scheduleId: backgroundWorkflowId(task.sessionId, task.id),
      spec: {
        cronExpressions: scheduleCrons(String(task.spec.cron)),
        timezone: String(task.spec.tz ?? agentTimezone()),
        ...(task.expiresAt ? { endAt: task.expiresAt } : {}),
      },
      action: {
        type: 'startWorkflow',
        workflowType: BACKGROUND_FIRE_WORKFLOW,
        workflowId: backgroundFireId(task.sessionId, task.id),
        taskQueue,
        // How often a busy session is asked again; BUSY_RETRY_MS unless a test sets less. Nothing of the task.
        args: retryMs === undefined ? [] : [{ retryMs }],
      },
      // A fire still waiting for a busy session takes the next occurrence's place, as the timer workflow did.
      policies: { overlap: 'SKIP', catchupWindow: CRON_CATCHUP },
    })
    return true
  } catch (err) {
    if (!(err instanceof ScheduleAlreadyRunning)) throw err
    return false
  }
}

/** Deletes a task's Schedule, if it has one; with `terminate`, its running fire is ended first. */
export async function unscheduleTask(client: Client, task: Pick<BackgroundTask, 'id' | 'sessionId'>, terminate?: string): Promise<void> {
  const handle = client.schedule.getHandle(backgroundWorkflowId(task.sessionId, task.id))
  try {
    if (terminate !== undefined) {
      for (const { workflow } of (await handle.describe()).info.runningActions) {
        await client.workflow
          .getHandle(workflow.workflowId)
          .terminate(terminate)
          .catch((err: unknown) => {
            if (!(err instanceof WorkflowNotFoundError)) throw err
          })
      }
    }
    await handle.delete()
  } catch (err) {
    if (!(err instanceof ScheduleNotFoundError)) throw err
  }
}

async function ended(promise: Promise<unknown>): Promise<void> {
  try {
    await promise
  } catch (err) {
    // Ended already, or never started: the row is what says it is over.
    if (!(err instanceof WorkflowNotFoundError)) throw err
  }
}

/** `retryMs`: how often a Schedule's fire asks a busy session again (BUSY_RETRY_MS by default; tests set less). */
export function temporalTaskRunner(client: Client, taskQueue: string, retryMs?: number): TaskRunner {
  return {
    async start(task) {
      if (isScheduled(task)) {
        const end = scheduleEnd(task)
        if (end !== undefined) return { end }
        await scheduleTask(client, task, taskQueue, retryMs)
        return
      }
      await client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(task.sessionId, task.id),
        taskQueue,
        // `recurring`: whether its loop asks to become a Schedule (bg_schedule); a one-shot job never does.
        args: [{ taskId: task.id, kind: task.kind, ...(task.kind === 'cron' ? { recurring: task.spec.recurring !== false } : {}) }],
      })
    },
    // Both, whichever it is: a cron job made before Step 9.8 is a workflow until it is converted.
    async cancel(task) {
      await unscheduleTask(client, task)
      await ended(client.workflow.getHandle(backgroundWorkflowId(task.sessionId, task.id)).cancel())
    },
    async terminate(task, reason) {
      await unscheduleTask(client, task, reason)
      await ended(client.workflow.getHandle(backgroundWorkflowId(task.sessionId, task.id)).terminate(reason))
    },
  }
}

export type CronPassDeps = { client: Client; store: Pick<BackgroundTaskStore, 'activeRecurringCrons' | 'finish' | 'get'>; taskQueue: string }

/**
 * Whether a task's Schedule has a fire running (one still waiting for a busy session): its
 * latest action's workflow, described, since `runningActions` stayed empty while a fire ran
 * (Temporal CLI 1.5 dev server, 2026-11-01).
 */
async function firing(client: Client, task: Pick<BackgroundTask, 'id' | 'sessionId'>): Promise<boolean> {
  let latest: string | undefined
  try {
    latest = (await client.schedule.getHandle(backgroundWorkflowId(task.sessionId, task.id)).describe()).info.recentActions.at(-1)?.action.workflow.workflowId
  } catch (err) {
    if (err instanceof ScheduleNotFoundError) return false
    throw err
  }
  if (latest === undefined) return false
  try {
    return (await client.workflow.getHandle(latest).describe()).status.name === 'RUNNING'
  } catch (err) {
    if (err instanceof WorkflowNotFoundError) return false
    throw err
  }
}

/** Ends a cron job's timer workflow made before Step 9.8, if it is still running. */
async function endTimerWorkflow(client: Client, task: Pick<BackgroundTask, 'id' | 'sessionId'>): Promise<void> {
  const handle = client.workflow.getHandle(backgroundWorkflowId(task.sessionId, task.id))
  try {
    if ((await handle.describe()).status.name !== 'RUNNING') return
  } catch (err) {
    if (err instanceof WorkflowNotFoundError) return
    throw err
  }
  await ended(handle.terminate('converted to a Schedule (plan 2026-10-13 Step 9.8)'))
}

/** One job of the pass; whether it made a Schedule. */
async function convertOne(deps: CronPassDeps, task: BackgroundTask, now: Date): Promise<boolean> {
  // Stored with syntax a Schedule cannot express: its timer workflow keeps firing it (service.ts isScheduled).
  if (!isScheduled(task)) return false
  const end = scheduleEnd(task, now)
  if (end !== undefined) {
    // A fire still waiting for a busy session sends, then ends the job itself (bg_next).
    if (await firing(deps.client, task)) return false
    await deps.store.finish(task.id, 'done', end)
    await unscheduleTask(deps.client, task)
    await endTimerWorkflow(deps.client, task)
    return false
  }
  const made = await scheduleTask(deps.client, task, deps.taskQueue)
  // Cancelled while the Schedule was being made (CronDelete deletes the Schedule after it ends the row).
  if (made && (await deps.store.get(task.id))?.state !== 'active') {
    await unscheduleTask(deps.client, task)
    return false
  }
  await endTimerWorkflow(deps.client, task)
  return made
}

/**
 * The pass over every active recurring cron job (decision Q11 of plan 2026-10-13 Step 9.8).
 * A job still fired by its `BackgroundTask` workflow becomes a Schedule, then the workflow is
 * ended. A job with no occurrence left before its expiry (its last one missed, as in a
 * Temporal outage longer than CRON_CATCHUP) is ended, and its Schedule deleted. Idempotent: a
 * job converted already keeps its Schedule, and its workflow is gone. Each job is its own: one
 * that fails leaves the rest to run, and the pass then throws, naming how many failed. How
 * many Schedules it made.
 */
export async function convertCronTasks(deps: CronPassDeps, now = new Date()): Promise<number> {
  let n = 0
  const failed: string[] = []
  for (const task of await deps.store.activeRecurringCrons()) {
    try {
      if (await convertOne(deps, task, now)) n += 1
    } catch (err) {
      failed.push(`${task.id}: ${err instanceof Error ? err.message : String(err)}`)
    }
  }
  if (failed.length > 0) throw new Error(`${failed.length} cron job${failed.length === 1 ? '' : 's'} not converted (${failed.join('; ')}); ${n} converted`)
  return n
}

/** How often the pass runs: it ends the jobs whose last occurrence was missed. */
export const CRON_PASS_MS = 60 * 60 * 1000
const CRON_PASS_FIRST_RETRY_MS = 5_000

/**
 * Runs convertCronTasks now and then every CRON_PASS_MS; a pass that fails (Temporal or the
 * database unreachable at start) is retried sooner, its wait doubling up to CRON_PASS_MS.
 * Returns what stops it.
 */
export function runCronPasses(deps: CronPassDeps, log: { info: (msg: string) => void; error: (msg: string) => void }): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopped = false
  const next = (inMs: number, retryMs: number): void => {
    if (stopped) return
    timer = setTimeout(() => run(retryMs), inMs)
    timer.unref?.()
  }
  const run = (retryMs: number): void => {
    void convertCronTasks(deps).then(
      (n) => {
        if (n > 0) log.info(`background: ${n} cron job${n === 1 ? '' : 's'} converted to Schedules`)
        next(CRON_PASS_MS, CRON_PASS_FIRST_RETRY_MS)
      },
      (err: unknown) => {
        log.error(`background: the cron pass failed, retried in ${Math.round(retryMs / 1000)} s: ${err instanceof Error ? err.message : String(err)}`)
        next(retryMs, Math.min(retryMs * 2, CRON_PASS_MS))
      },
    )
  }
  run(CRON_PASS_FIRST_RETRY_MS)
  return () => {
    stopped = true
    clearTimeout(timer)
  }
}
