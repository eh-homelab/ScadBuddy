import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { Inbox, userMessage } from '../src/harness/inbox.js'
import type { HarnessRun } from '../src/harness/run.js'
import { scriptedRunner } from './support/sessions.js'

// The fake Claude Code (test/support/sessions.ts) takes an interrupt as the real one
// reads stdin, in order (#3661): after a pushed message, for that message's turn.

const process = (inbox: Inbox, next: Parameters<typeof scriptedRunner>[0]) => {
  const { runner } = scriptedRunner(next)
  const q = runner({ prompt: inbox, credential: { kind: 'anthropic_api_key', secret: 'k' } } as unknown as HarnessRun)
  return { q, it: q[Symbol.asyncIterator]() }
}

/** The next result, or `stuck` when none comes in time. */
async function nextResult(it: AsyncIterator<SDKMessage>): Promise<SDKMessage | 'stuck'> {
  const deadline = new Promise<'stuck'>((resolve) => setTimeout(() => resolve('stuck'), 1000).unref())
  for (;;) {
    const m = await Promise.race([it.next(), deadline])
    if (m === 'stuck') return m
    if (m.done) return 'stuck'
    if (m.value.type === 'result') return m.value
  }
}

describe('scriptedRunner', () => {
  it('ends a turn interrupted after its message was pushed and before the fake read it', async () => {
    const inbox = new Inbox()
    const { q, it: messages } = process(inbox, () => ({ hang: true }))
    inbox.push(userMessage('go'))
    // Nothing has read the inbox yet: the interrupt lands between the push and the read.
    await q.interrupt()
    expect(await nextResult(messages)).toMatchObject({ type: 'result', subtype: 'error_during_execution' })
    q.close()
  })

  it('ignores an interrupt when no turn runs and nothing is waiting', async () => {
    const inbox = new Inbox()
    let n = 0
    const { q, it: messages } = process(inbox, () => (n++ === 0 ? { reply: 'one' } : { hang: true }))
    inbox.push(userMessage('first'))
    expect(await nextResult(messages)).toMatchObject({ type: 'result', subtype: 'success' })
    // Idle, with nothing pushed: as Claude Code's, the interrupt is nothing.
    await q.interrupt()
    inbox.push(userMessage('second'))
    expect(await nextResult(messages)).toBe('stuck')
    q.close()
  })

  it('does not carry an interrupt the turn never asked for into the next turn', async () => {
    const inbox = new Inbox()
    let n = 0
    const { q, it: messages } = process(inbox, () => (n++ === 0 ? { reply: 'one' } : { hang: true }))
    inbox.push(userMessage('first'))
    // Read and replying: the interrupt is this turn's, which ends on its own.
    const first = await messages.next()
    expect(first.done).toBe(false)
    await q.interrupt()
    expect(await nextResult(messages)).toMatchObject({ type: 'result', subtype: 'success' })
    inbox.push(userMessage('second'))
    expect(await nextResult(messages)).toBe('stuck')
    q.close()
  })
})
