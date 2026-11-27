import { proxyActivities } from '@temporalio/workflow'

// A stand-in for a workflow that calls one tool on `agent-tools` by name: a flow run's,
// or a session's ToolCall (`tool:<name>`), with the activity ID `tool-<tool_use_id>`.

export async function callTool(name: string, input: unknown, toolUseId: string): Promise<unknown> {
  const tools = proxyActivities<Record<string, (input: unknown) => Promise<unknown>>>({
    taskQueue: 'agent-tools',
    startToCloseTimeout: '1 minute',
    retry: { maximumAttempts: 1 },
    activityId: `tool-${toolUseId}`,
  })
  return tools[name]!(input)
}

/** Runs `name` on `taskQueue` with a heartbeat timeout of `heartbeatMs`, and no retry. */
export async function callBeating(name: string, heartbeatMs: number, taskQueue: string): Promise<unknown> {
  const activities = proxyActivities<Record<string, () => Promise<unknown>>>({
    taskQueue,
    startToCloseTimeout: '1 minute',
    heartbeatTimeout: heartbeatMs,
    retry: { maximumAttempts: 1 },
  })
  return activities[name]!()
}
