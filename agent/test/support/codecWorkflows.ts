import { condition, defineSignal, proxyActivities, setHandler } from '@temporalio/workflow'

// Stand-ins for a durable session's workflow (spec 2026-10-01 §6.5): one holds the
// user's words in its input and returns them when told to; one calls a tool that fails
// with the words in its error. A test reads their histories with and without the codec.

export const finish = defineSignal('finish')

export async function holdText(text: string): Promise<string> {
  let done = false
  setHandler(finish, () => {
    done = true
  })
  await condition(() => done)
  return `${text} (done)`
}

const { failWith } = proxyActivities<{ failWith(text: string): Promise<void> }>({
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
