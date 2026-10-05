import { condition, defineQuery, defineUpdate, isCancellation, setHandler } from '@temporalio/workflow'

// A TypeScript stand-in for agent-durable's DurableSession (scadbuddy_durable/workflow.py),
// with its names, so test/durable.temporal.test.ts checks the wire shape of the agent
// service's calls without Python. It records what it receives; a Stop (cancellation)
// returns a state the next execution must be started with.

export type Seen = { args: unknown[]; messages: unknown[]; reviews: unknown[] }

export const seenQuery = defineQuery<Seen>('seen')

export async function DurableSession(input: unknown, state: unknown, inbox: unknown): Promise<unknown> {
  const messages: unknown[] = []
  const reviews: unknown[] = []
  setHandler(
    defineUpdate<void, [unknown]>('send_message'),
    (message) => {
      messages.push(message)
    },
    {
      validator: (_message: unknown) => {
        if (messages.length >= 2) throw new Error('the session is busy')
      },
    },
  )
  setHandler(
    defineUpdate<void, [string, boolean, string]>('review'),
    (...decision) => {
      reviews.push(decision)
    },
    {
      validator: (toolUseId: string, _approved: boolean, _approver: string) => {
        if (toolUseId !== 'toolu_1') throw new Error(`No tool call ${toolUseId} is waiting for approval`)
      },
    },
  )
  setHandler(defineQuery<unknown[]>('pending_approvals'), () => [
    { id: 'toolu_1', name: 'send_to_bambuddy', input: { output: 'box.3mf' } },
  ])
  setHandler(seenQuery, () => ({ args: [input, state, inbox], messages, reviews }))
  try {
    await condition(() => false)
  } catch (err) {
    if (isCancellation(err)) return { handed_over: messages.length }
    throw err
  }
  return null
}
