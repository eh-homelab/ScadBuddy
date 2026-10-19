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

/**
 * A call under a heartbeat timeout shorter than the activity runs (#4261): it completes only
 * if the worker beats for an activity that never beats itself.
 */
export async function callUnderHeartbeat(name: string, heartbeatTimeout: string): Promise<unknown> {
  const tools = proxyActivities<Record<string, () => Promise<unknown>>>({
    taskQueue: 'agent-tools',
    startToCloseTimeout: '1 minute',
    heartbeatTimeout,
    retry: { maximumAttempts: 1 },
  })
  return tools[name]!()
}
