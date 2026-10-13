import { ApplicationFailure, condition, defineQuery, defineSignal, defineUpdate, setHandler } from '@temporalio/workflow'

// A stand-in for agent-durable's DurableSession (plan 5c PR 3): what the agent service's
// dispatch (src/sessions/durable.ts) sends it, kept for the test to read. The
// `send_message` Update's validator refuses a second turn `busy`, as the real one does;
// `cancel_input` and the `interrupt` Signal are recorded in the order they came. With
// `hold_cancel` signalled, `cancel_input` never answers (a worker that is down). The
// `turn` Query names the turn it holds, None between turns, as the real one does.
// `fork_point` (#2688) serves the conversation `set_conversation` gave it, or the one it
// was started with (a fork's), a page at a time as the real one does.

type Message = { turn_id: string; text: string; author: unknown; images: { name: string; mediaType: string }[] }

export const sendMessage = defineUpdate<{ accepted: boolean; turn_id: string }, [Message]>('send_message')
export const cancelInput = defineUpdate<'cancelled' | 'none', [{ reason: string }]>('cancel_input')
export const interrupt = defineSignal<[{ reason: string }]>('interrupt')
export const holdCancel = defineSignal('hold_cancel')
export const endTurn = defineSignal('end_turn')
export const recorded = defineQuery<{ start: unknown; messages: Message[]; calls: string[] }>('recorded')
export const turn = defineQuery<string | null>('turn')
type Conversation = { session_id: string; checkpoint: string; conversation: string[] }
type ForkPoint = { session_id: string | null; checkpoint: string | null; busy: boolean; total: number; entries: string[] }
export const setConversation = defineSignal<[Conversation & { busy?: boolean }]>('set_conversation')
export const forkPoint = defineQuery<ForkPoint, [{ start: number; limit: number }]>('fork_point')

export async function DurableSession(start: unknown): Promise<void> {
  const messages: Message[] = []
  const calls: string[] = []
  let busy = false
  let hold = false
  let held: (Conversation & { busy?: boolean }) | undefined = (start as { agent?: Conversation } | null)?.agent ?? undefined
  setHandler(recorded, () => ({ start, messages, calls }))
  setHandler(setConversation, (conversation) => {
    held = conversation
  })
  setHandler(forkPoint, ({ start: from, limit }) => {
    const texts = held?.conversation ?? []
    let end = from
    let used = 0
    while (end < texts.length && (end === from || used + texts[end]!.length <= limit)) used += texts[end++]!.length
    return {
      session_id: held?.session_id ?? null,
      checkpoint: held?.checkpoint ?? null,
      busy: busy || held?.busy === true,
      total: texts.length,
      entries: texts.slice(from, end),
    }
  })
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
  setHandler(holdCancel, () => {
    hold = true
  })
  setHandler(endTurn, () => {
    busy = false
  })
  await condition(() => false)
}
