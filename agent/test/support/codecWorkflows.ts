import { condition, defineSignal, setHandler } from '@temporalio/workflow'

// A stand-in for a durable session's workflow (spec 2026-10-01 §6.5): it holds the
// user's words in its input and returns them when told to, so a test can read its
// history with and without the payload codec.

export const finish = defineSignal('finish')

export async function holdText(text: string): Promise<string> {
  let done = false
  setHandler(finish, () => {
    done = true
  })
  await condition(() => done)
  return `${text} (done)`
}
