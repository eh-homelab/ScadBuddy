import { type Client, ScheduleAlreadyRunning, ScheduleNotFoundError, WorkflowNotFoundError } from '@temporalio/client'
import { agentTimezone } from './schedule.js'
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

/** Creates a recurring cron job's Schedule; one already there is left as it is (idempotent). Whether it made one. */
export async function scheduleTask(client: Client, task: StartedTask, taskQueue: string, retryMs?: number): Promise<boolean> {
  try {
    await client.schedule.create({
      scheduleId: backgroundWorkflowId(task.sessionId, task.id),
      spec: {
        cronExpressions: [String(task.spec.cron)],
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
        await scheduleTask(client, task, taskQueue, retryMs)
        return
      }
      await client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(task.sessionId, task.id),
        taskQueue,
        args: [{ taskId: task.id, kind: task.kind }],
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

/**
 * The start-time pass of decision Q11: every active recurring cron job still fired by its
 * `BackgroundTask` workflow becomes a Schedule, then the workflow is ended. Idempotent: a job
 * converted already keeps its Schedule, and its workflow is gone. How many Schedules it made.
 */
export async function convertCronTasks(deps: { client: Client; store: Pick<BackgroundTaskStore, 'activeRecurringCrons'>; taskQueue: string }): Promise<number> {
  let n = 0
  for (const task of await deps.store.activeRecurringCrons()) {
    if (await scheduleTask(deps.client, task, deps.taskQueue)) n += 1
    await ended(deps.client.workflow.getHandle(backgroundWorkflowId(task.sessionId, task.id)).terminate('converted to a Schedule (plan 2026-10-13 Step 9.8)'))
  }
  return n
}
