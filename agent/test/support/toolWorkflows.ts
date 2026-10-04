import { condition, defineUpdate, proxyActivities, setHandler } from '@temporalio/workflow'

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

/** For the client-side codec gate (spec §6.5): a workflow that takes Updates. */
export const sendUpdate = defineUpdate<string, [string]>('send')

export async function turns(first: string): Promise<string> {
  let last = first
  setHandler(sendUpdate, (text) => {
    last = text
    return `got ${text}`
  })
  await condition(() => last === 'stop')
  return last
}
