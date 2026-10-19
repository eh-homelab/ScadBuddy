import { proxyActivities } from '@temporalio/workflow'

// A stand-in for the durable session's workflow (phase 5): it calls one tool on
// `agent-tools` by name, with the activity ID `activity_as_tool` gives it.

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
