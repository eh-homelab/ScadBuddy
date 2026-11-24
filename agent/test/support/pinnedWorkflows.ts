import { proxyActivities } from '@temporalio/workflow'

// A pinned call on `agent-tools.<host>` (#2320): for
// test/sessionAffinity.temporal.test.ts, which checks an AgentSession turn on the same replica
// never holds it up. It gets 5 s to start, so one held up fails.

export async function pinnedCall(queue: string): Promise<string> {
  const pinned = proxyActivities<{ pinned_probe: () => Promise<string> }>({
    taskQueue: queue,
    scheduleToStartTimeout: '5s',
    startToCloseTimeout: '10s',
    retry: { maximumAttempts: 1 },
  })
  return pinned.pinned_probe()
}
