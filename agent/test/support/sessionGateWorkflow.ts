import { CancellationScope, condition, defineQuery, defineSignal, defineUpdate, setHandler, workflowInfo } from '@temporalio/workflow'
import { durableRequestId } from '../../src/gate/ids.js'
import type { Owner } from '../../src/sessions/protocol.js'
import { type GateCall, type GateHost, type GateSettings, type GateTurn, type Resolution, SessionGate } from '../../src/temporal/session/gate.js'

// A workflow that wraps SessionGate, for sessionGate.temporal.test.ts: the host is a
// record of what the gate decided (what AgentSession's turn will be), calls arrive by
// the `call` Signal, and the gate's Update, Query and validator are installed as they
// are in a session.

export type HarnessInput = {
  sessionId: string
  owner: Owner
  creator: Owner
  turn?: GateTurn
  answerTools?: string[]
  turnTools?: string[]
  settings?: GateSettings
  taskQueue?: string
  grantCheckWait?: string
}

export type Decision = { callId: string; allow: boolean; approver?: string | undefined }
export type HarnessState = { decisions: Decision[]; refusals: Record<string, string>; resolutions: Record<string, Resolution> }

export const callSignal = defineSignal<[GateCall]>('call')
export const finishSignal = defineSignal<[]>('finish')
export const stateQuery = defineQuery<HarnessState>('state')
export const cancelAllUpdate = defineUpdate<number, [{ reason: string; bounded?: boolean }]>('cancel_all')
export const settleUpdate = defineUpdate<void, [{ reason: string }]>('settle')
export const loadSettingsUpdate = defineUpdate<GateSettings, []>('load_settings')
export const ridQuery = defineQuery<string, [string]>('rid')

export async function gateHarness(input: HarnessInput): Promise<HarnessState> {
  const calls: GateCall[] = []
  const waiting = new Set<string>()
  const state: HarnessState = { decisions: [], refusals: {}, resolutions: {} }
  let finished = false
  const host: GateHost = {
    decide(callId, allow, approver) {
      if (!waiting.delete(callId)) return
      state.decisions.push({ callId, allow, approver })
    },
    validateDecision(callId) {
      if (!waiting.has(callId)) throw new Error(`${callId} does not wait`)
    },
    refuse(callId, message) {
      state.refusals[callId] = message
    },
    turn: () => input.turn,
  }
  const gate = new SessionGate({
    sessionId: input.sessionId,
    owner: input.owner,
    creator: input.creator,
    host,
    kindOf: (tool) => (input.answerTools?.includes(tool) ? 'answer' : 'approval'),
    turnTools: () => new Set(input.turnTools ?? []),
    ...(input.taskQueue ? { taskQueue: input.taskQueue } : {}),
    ...(input.grantCheckWait ? { grantCheckWait: input.grantCheckWait } : {}),
    ...(input.settings ? { settings: input.settings } : {}),
  })
  gate.install()
  setHandler(stateQuery, () => state)
  setHandler(ridQuery, (callId) => durableRequestId(input.sessionId, workflowInfo().runId, callId))
  setHandler(finishSignal, () => {
    finished = true
  })
  setHandler(callSignal, (call) => {
    calls.push(call)
    waiting.add(call.id)
    const rid = durableRequestId(input.sessionId, workflowInfo().runId, call.id)
    void gate.resolution(rid).then((r) => {
      state.resolutions[rid] = r
    })
  })
  setHandler(cancelAllUpdate, ({ reason, bounded }) => gate.cancelAll(reason, bounded))
  setHandler(settleUpdate, ({ reason }) => gate.settle(reason))
  setHandler(loadSettingsUpdate, () => gate.loadSettings())
  const watching = new CancellationScope()
  void watching.run(() => gate.watch(() => calls.filter((c) => waiting.has(c.id)))).catch(() => {})
  await condition(() => finished)
  watching.cancel()
  return state
}
