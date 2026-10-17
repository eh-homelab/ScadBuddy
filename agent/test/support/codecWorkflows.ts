import { condition, defineQuery, defineSignal, proxyActivities, setHandler } from '@temporalio/workflow'

// Stand-ins for a durable session's workflow (spec 2026-10-01 §6.5): one holds the
// user's words in its input and returns them when told to; one calls a tool that fails
// with the words in its error. A test reads their histories with and without the codec.

export const finish = defineSignal('finish')
/** The words it holds, as a fork reads a session's conversation (#3739). */
export const heldText = defineQuery<string>('held_text')

export async function holdText(text: string): Promise<string> {
  let done = false
  setHandler(finish, () => {
    done = true
  })
  setHandler(heldText, () => text)
  await condition(() => done)
  return `${text} (done)`
}

const { failWith, lengthOf } = proxyActivities<{ failWith(text: string): Promise<void>; lengthOf(text: string): Promise<number> }>({
  startToCloseTimeout: '1 minute',
  retry: { maximumAttempts: 1 },
})

export async function failingTool(text: string): Promise<string> {
  try {
    await failWith(text)
    return 'ran'
  } catch (err) {
    const cause = (err as Error).cause
    return `caught: ${cause instanceof Error ? cause.message : ''}`
  }
}

/**
 * Hands a tool `chars` characters, as a turn hands a large tool input over (#3739).
 * Built here, so the workflow's own input and result stay small: the only large
 * payload is the activity's input.
 */
export async function toolInput(chars: number): Promise<number> {
  return await lengthOf('x'.repeat(chars))
}
