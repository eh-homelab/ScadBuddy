import { CancellationScope, condition, defineQuery, defineUpdate, isCancellation, setHandler, sleep } from '@temporalio/workflow'

// A TypeScript stand-in for agent-durable's DurableSession (scadbuddy_durable/workflow.py),
// with its names, so test/durable.temporal.test.ts checks the wire shape of the agent
// service's calls without Python. It records what it receives; a Stop (cancellation)
// returns a state the next execution must be started with; with `stop_ms` in its input, only
// that long after the Stop, as the plugin ends its task before the execution returns. Like
// the Python workflow, a nudge for a message it already took is a no-op.

export type Seen = { args: unknown[]; messages: unknown[]; reviews: unknown[] }

export const seenQuery = defineQuery<Seen>('seen')

export async function DurableSession(input: unknown, state: unknown): Promise<unknown> {
  const messages: { id: string }[] = []
  const reviews: unknown[] = []
  const known = (nudge: { id: string }) => messages.some((m) => m.id === nudge.id)
  setHandler(
    defineUpdate<void, [{ id: string }]>('send_message'),
    (nudge) => {
      if (!known(nudge)) messages.push(nudge)
    },
    {
      validator: (nudge: { id: string }) => {
        if (messages.length >= 2 && !known(nudge)) throw new Error('the session is busy')
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
  setHandler(seenQuery, () => ({ args: [input, state], messages, reviews }))
  try {
    await condition(() => false)
  } catch (err) {
    if (isCancellation(err)) {
      const stopMs = (input as { stop_ms?: number }).stop_ms
      if (stopMs) await CancellationScope.nonCancellable(() => sleep(stopMs))
      return { handed_over: messages.length }
    }
    throw err
  }
  return null
}
