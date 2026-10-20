import { ApplicationFailure, condition, defineQuery, defineSignal, defineUpdate, setHandler } from '@temporalio/workflow'

// A stand-in for agent-durable's DurableSession (plan 5c PR 3): what the agent service's
// dispatch (src/sessions/durable.ts) sends it, kept for the test to read. The
// `send_message` Update's validator refuses a second turn `busy`, as the real one does;
// `cancel_input` and the `interrupt` Signal are recorded in the order they came. With
// `hold_cancel` signalled, `cancel_input` never answers (a worker that is down). The
// `turn` Query names the turn it holds, None between turns, as the real one does.
// `fork_point` (#2688) serves the conversation `set_conversation` gave it, or the one it
// was started with (a fork's), a page at a time as the real one does, with what the agent
// still owes at its checkpoint on the first page (#4173). `page_chars` makes its pages
// smaller than the agent asks, and `owed_reads` answers each first-page read with the next
// owed state in turn (the last one after), as a parent whose turn stops between reads. That
// count is kept by the `fork_point` Query, which a real Query never does, and is in no
// history: it holds only while the worker's cache keeps the workflow, and starts again if
// the workflow is evicted and replayed mid-test (the test's worker sets `maxCachedWorkflows`
// so none is).

type Message = { turn_id: string; text: string; author: unknown; images: { name: string; mediaType: string }[] }

export const sendMessage = defineUpdate<{ accepted: boolean; turn_id: string }, [Message]>('send_message')
export const cancelInput = defineUpdate<'cancelled' | 'none', [{ reason: string }]>('cancel_input')
export const interrupt = defineSignal<[{ reason: string }]>('interrupt')
export const holdCancel = defineSignal('hold_cancel')
export const endTurn = defineSignal('end_turn')
export const recorded = defineQuery<{ start: unknown; messages: Message[]; calls: string[]; forkPointPage0Queries: number }>('recorded')
export const turn = defineQuery<string | null>('turn')
type Owed = { pending?: Record<string, unknown>; fork_next?: boolean; recent_call_ids?: string[] }
type Conversation = { session_id: string; checkpoint: string; conversation: string[] } & Owed
type ForkPoint = { session_id: string | null; checkpoint: string | null; busy: boolean; total: number; entries: string[] } & Owed
type Held = Conversation & { busy?: boolean; page_chars?: number; owed_reads?: Owed[] }
export const setConversation = defineSignal<[Held]>('set_conversation')
export const forkPoint = defineQuery<ForkPoint, [{ start: number; limit: number }]>('fork_point')

export async function DurableSession(start: unknown): Promise<void> {
  const messages: Message[] = []
  const calls: string[] = []
  let busy = false
  let hold = false
  let held: Held | undefined = (start as { agent?: Conversation } | null)?.agent ?? undefined
  let forkPointPage0Queries = 0
  setHandler(recorded, () => ({ start, messages, calls, forkPointPage0Queries }))
  setHandler(setConversation, (conversation) => {
    held = conversation
    forkPointPage0Queries = 0
  })
  setHandler(forkPoint, ({ start: from, limit: asked }) => {
    const texts = held?.conversation ?? []
    const limit = held?.page_chars ?? asked
    const reads = held?.owed_reads
    const owed = from === 0 && reads?.length ? reads[Math.min(forkPointPage0Queries++, reads.length - 1)]! : held
    let end = from
    let used = 0
    while (end < texts.length && (end === from || used + texts[end]!.length <= limit)) used += texts[end++]!.length
    return {
      session_id: held?.session_id ?? null,
      checkpoint: held?.checkpoint ?? null,
      busy: busy || held?.busy === true,
      total: texts.length,
      entries: texts.slice(from, end),
      ...(from === 0 ? { pending: owed?.pending ?? {}, fork_next: owed?.fork_next ?? false, recent_call_ids: owed?.recent_call_ids ?? [] } : {}),
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
