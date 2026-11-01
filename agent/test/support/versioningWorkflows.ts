import { ActivityFailure, ApplicationFailure, condition, defineSignal, proxyActivities, setHandler } from '@temporalio/workflow'

// Probe workflows for test/temporal.versioning.temporal.test.ts (plan 2026-10-13 Phase 11):
// a caller that runs one named activity on a named queue, once, and says which build ran it
// or why it failed. Registered on a versioned queue (as AgentSession is), an unversioned one
// (an in-process ProjectWorkflow) and a queue of another deployment (prod's ProjectWorkflow).

export type ProbeCall = { queue: string; activity: string; scheduleToStartSeconds?: number }
export type ProbeResult = { ran: string } | { failed: string }

export async function probeCaller(call: ProbeCall): Promise<ProbeResult> {
  const acts = proxyActivities<Record<string, () => Promise<string>>>({
    taskQueue: call.queue,
    scheduleToStartTimeout: `${call.scheduleToStartSeconds ?? 20}s`,
    startToCloseTimeout: '10s',
    retry: { maximumAttempts: 1 },
  })
  try {
    return { ran: await acts[call.activity]!() }
  } catch (err) {
    const cause = err instanceof ActivityFailure ? err.cause : err
    return { failed: cause instanceof ApplicationFailure ? `${cause.type}: ${cause.message}` : String((cause as Error).message) }
  }
}

export const goSignal = defineSignal('go')

/** Started before its queue is versioned, as an open BackgroundTask is at the first versioned deploy; runs `call` once signalled. */
export async function probeWaiter(call: ProbeCall): Promise<ProbeResult> {
  let go = false
  setHandler(goSignal, () => void (go = true))
  await condition(() => go)
  return probeCaller(call)
}
