import { proxyActivities } from '@temporalio/workflow'

// A DurableSession's pinned call, as session/tools.py schedules one on `agent-tools.<host>`
// (#2320): for test/sessionAffinity.temporal.test.ts, which checks an AgentSession turn on the
// same replica never holds it up. The 5 s to start is the `turn_tools` listing's HOST_WAIT.

export async function pinnedCall(queue: string): Promise<string> {
  const pinned = proxyActivities<{ pinned_probe: () => Promise<string> }>({
    taskQueue: queue,
    scheduleToStartTimeout: '5s',
    startToCloseTimeout: '10s',
    retry: { maximumAttempts: 1 },
  })
  return pinned.pinned_probe()
}
