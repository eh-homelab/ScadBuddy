import { type Client, WorkflowNotFoundError } from '@temporalio/client'
import { BACKGROUND_WORKFLOW, backgroundWorkflowId, type TaskRunner } from './service.js'

/** Background tasks on Temporal: one `BackgroundTask` workflow per task, on `taskQueue`. */
export function temporalTaskRunner(client: Client, taskQueue: string): TaskRunner {
  return {
    async start(task) {
      await client.workflow.start(BACKGROUND_WORKFLOW, {
        workflowId: backgroundWorkflowId(task.id),
        taskQueue,
        args: [{ taskId: task.id, kind: task.kind }],
      })
    },
    async cancel(taskId) {
      try {
        await client.workflow.getHandle(backgroundWorkflowId(taskId)).cancel()
      } catch (err) {
        // Ended already, or never started: the row is what says it is over.
        if (!(err instanceof WorkflowNotFoundError)) throw err
      }
    },
  }
}
