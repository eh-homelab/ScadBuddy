import { type Client, WorkflowNotFoundError } from '@temporalio/client'
import { BACKGROUND_WORKFLOW, backgroundWorkflowId, type TaskRunner } from './service.js'

/** Background tasks on Temporal: one `BackgroundTask` workflow per task, on `taskQueue`. */
export function temporalTaskRunner(client: Client, taskQueue: string): TaskRunner {
  return {
    async start(task) {
      await client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(task.sessionId, task.id),
        taskQueue,
        args: [{ taskId: task.id }],
      })
    },
    async cancel(task) {
      try {
        await client.workflow.getHandle(backgroundWorkflowId(task.sessionId, task.id)).cancel()
      } catch (err) {
        // Ended already, or never started: the row is what says it is over.
        if (!(err instanceof WorkflowNotFoundError)) throw err
      }
    },
    async terminate(task, reason) {
      try {
        await client.workflow.getHandle(backgroundWorkflowId(task.sessionId, task.id)).terminate(reason)
      } catch (err) {
        if (!(err instanceof WorkflowNotFoundError)) throw err
      }
    },
  }
}
