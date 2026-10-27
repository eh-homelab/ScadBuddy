import { ActivityFailure, isCancellation, proxyActivities, TimeoutFailure } from '@temporalio/workflow'
import type { CallOutcome } from '../bridge/protocol.js'

// A browser_* call for a tab whose bridge socket is on another agent replica (#4780,
// bridge/relay.ts). The workflow runs on the shared `agent-tools` queue, which every
// replica polls; its one activity runs on the queue of the replica holding the tab
// (`agent-tools.<identity>`, ai_tab_owners), so Temporal delivers the call to that
// replica only, once. The result comes back as the activity's result. In the workflow
// bundle: no Node imports.

export const BRIDGE_CALL_WORKFLOW = 'BridgeCall'
/** The owner's side: runs a request on a tab it holds (bridge/relay.ts `activities`). */
export const BRIDGE_TAB_ACTIVITY = 'bridge.tab'

/** What a replica is asked to do with a tab it holds. */
export type RelayRequest =
  | { op: 'call'; tool: string; args: Record<string, unknown>; timeoutMs: number }
  | { op: 'status' }

/** The owner's answer: the tab's outcome, its state, or that the tab was gone by the time it was asked. */
export type RelayAnswer =
  | { op: 'call'; outcome: CallOutcome }
  | { op: 'status'; route: string; live: string[] }
  | { op: 'gone' }

export type BridgeCallInput = {
  /** The owner's queue (ai_tab_owners). */
  queue: string
  tabId: string
  request: RelayRequest
  /** How long the owner has to take the call (schedule-to-start); past it, nobody holds the tab. */
  ackTimeoutMs: number
}

/** The owner's answer, or `unreachable`: no replica polls its queue (the pod is gone). */
export type BridgeCallAnswer = RelayAnswer | { op: 'unreachable' }

/** How long the owner may run a request: the tab's own timeout, then the ack timeout for the way back. */
export const ownerRunMs = (input: Pick<BridgeCallInput, 'request' | 'ackTimeoutMs'>): number =>
  (input.request.op === 'call' ? input.request.timeoutMs : 0) + input.ackTimeoutMs

export async function BridgeCall(input: BridgeCallInput): Promise<BridgeCallAnswer> {
  const runMs = ownerRunMs(input)
  const owner = proxyActivities<Record<string, (tabId: string, request: RelayRequest) => Promise<RelayAnswer>>>({
    taskQueue: input.queue,
    scheduleToStartTimeout: input.ackTimeoutMs,
    startToCloseTimeout: runMs,
    // Once: a call may have acted on the tab (a click, a navigation) before its answer was lost.
    retry: { maximumAttempts: 1 },
  })
  try {
    return await owner[BRIDGE_TAB_ACTIVITY]!(input.tabId, input.request)
  } catch (err) {
    // The caller cancelled: the workflow ends cancelled, as asked.
    if (isCancellation(err) || !(err instanceof ActivityFailure)) throw err
    const cause = err.cause
    if (cause instanceof TimeoutFailure && cause.timeoutType === 'SCHEDULE_TO_START') return { op: 'unreachable' }
    if (input.request.op !== 'call') return { op: 'gone' }
    // The owner took the request and went away, stopped answering, or failed while it ran it:
    // the tab may still act. An answer, as the old relay's (no_answer), never a failed workflow.
    const message =
      cause instanceof TimeoutFailure
        ? `the agent replica holding the tab did not answer within ${Math.round(runMs / 1000)} s; `
        : 'the agent replica holding the tab failed while it ran the call; '
    return {
      op: 'call',
      outcome: {
        ok: false,
        error: { code: 'no_answer', message: `${message}the call may still finish, so take a snapshot before trying again` },
      },
    }
  }
}
