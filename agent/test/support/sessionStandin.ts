import { ApplicationFailure, condition, defineQuery, defineSignal, defineUpdate, setHandler } from '@temporalio/workflow'

// A stand-in for AgentSession (temporal/session/workflow.ts) that runs no turn: what the agent
// service's dispatch (src/sessions/durable.ts) sends it, kept for the test to read. The
// `send_message` Update's validator refuses a second turn `busy`, as the real one does;
// `cancel_input`, the `interrupt` Signal and the `owner_changed` Signal are recorded in the order
// they came. With `hold_cancel` signalled, `cancel_input` never answers (a worker that is down).
// The `turn` Query names the turn it holds, null between turns. `end_turn` stands for the turn's
// end, and `end` (#1056) closes it once no turn runs, as the real one's does.

type Message = { turn_id: string; text: string; author: unknown; images: { name: string; mediaType: string }[] }

export const sendMessage = defineUpdate<{ accepted: boolean; turn_id: string }, [Message]>('send_message')
export const cancelInput = defineUpdate<'cancelled' | 'none', [{ reason: string }]>('cancel_input')
export const interrupt = defineSignal<[{ reason: string }]>('interrupt')
export const ownerChanged = defineSignal<[{ owner: { kind: string; id: string } }]>('owner_changed')
export const holdCancel = defineSignal('hold_cancel')
export const endTurn = defineSignal('end_turn')
export const end = defineSignal<[unknown?]>('end')
export const recorded = defineQuery<{ start: unknown; messages: Message[]; calls: string[] }>('recorded')
export const turn = defineQuery<string | null>('turn')

export async function AgentSession(start: unknown): Promise<void> {
  const messages: Message[] = []
  const calls: string[] = []
  let busy = false
  let hold = false
  let ended = false
  setHandler(end, () => {
    calls.push('end')
    ended = true
  })
  setHandler(recorded, () => ({ start, messages, calls }))
  setHandler(turn, () => (busy ? (messages.at(-1)?.turn_id ?? null) : null))
  setHandler(
    sendMessage,
    (message) => {
      messages.push(message)
      busy = true
      return { accepted: true, turn_id: message.turn_id }
    },
    {
      validator: (_message: Message) => {
        if (busy) throw ApplicationFailure.nonRetryable('the session is running a turn', 'busy')
      },
    },
  )
  setHandler(cancelInput, async ({ reason }) => {
    calls.push(`cancel_input:${reason}`)
    if (hold) await condition(() => false)
    return 'none' as const
  })
  setHandler(interrupt, ({ reason }) => {
    calls.push(`interrupt:${reason}`)
    busy = false
  })
  setHandler(ownerChanged, ({ owner }) => {
    calls.push(`owner_changed:${owner.kind}:${owner.id}`)
  })
  setHandler(holdCancel, () => {
    hold = true
  })
  setHandler(endTurn, () => {
    busy = false
  })
  await condition(() => ended && !busy)
}
