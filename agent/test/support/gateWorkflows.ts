import { ApplicationFailure, condition, defineQuery, defineSignal, defineUpdate, setHandler } from '@temporalio/workflow'

// A stand-in for DurableSession's gate handlers (spec 2026-10-01 §6.6; the real one is
// phase 5c, in agent-durable): one parked entry, the `pending_input` Query, the
// `respond` Update with a validator that refuses as the real one does
// (ApplicationFailure `GateRefused:<code>`), the `cancel_input` Update and the
// cancel-only `interrupt` Signal. Names as in src/gate/names.ts.

type Entry = { id: string; kind: 'approval' | 'answer'; state: 'pending' | 'resolved' }
type RespondArgs = { request_id: string; response: { kind: string; decision?: string }; responder: { kind: string; id: string }; role: string }

export const pendingInput = defineQuery<unknown[]>('pending_input')
export const respond = defineUpdate<{ kind: string; outcome: string }, [RespondArgs]>('respond')
export const cancelInput = defineUpdate<'cancelled' | 'none', [{ reason: string }]>('cancel_input')
export const interrupt = defineSignal<[{ reason: string }]>('interrupt')

export async function gateStandIn(entryId: string, kind: 'approval' | 'answer'): Promise<string> {
  const entry: Entry = { id: entryId, kind, state: 'pending' }
  let interrupted: string | undefined
  setHandler(pendingInput, () =>
    entry.state === 'pending'
      ? [{ id: entry.id, kind: entry.kind, session_id: null, tool: 'print_output', summary: '{}', input_hash: null, prompt: '', requested_by: null, responders: ['browser'], created_at: '', expires_at: '' }]
      : [],
  )
  setHandler(
    respond,
    (args) => {
      entry.state = 'resolved'
      return { kind: entry.kind, outcome: args.response.decision === 'approve' ? 'approved' : 'denied' }
    },
    {
      validator: (args) => {
        if (args.request_id !== entry.id) throw ApplicationFailure.nonRetryable('stale', 'GateRefused:stale')
        if (entry.state !== 'pending') throw ApplicationFailure.nonRetryable('already resolved', 'GateRefused:resolved')
        if (args.role === 'owner') throw ApplicationFailure.nonRetryable('not yours', 'GateRefused:forbidden')
      },
    },
  )
  setHandler(cancelInput, () => {
    const was = entry.state
    entry.state = 'resolved'
    return was === 'pending' ? 'cancelled' : 'none'
  })
  setHandler(interrupt, ({ reason }) => {
    interrupted = reason
    entry.state = 'resolved'
  })
  await condition(() => interrupted !== undefined)
  return interrupted ?? ''
}
