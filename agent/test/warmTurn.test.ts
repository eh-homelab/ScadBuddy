import type { SDKMessage, SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it, vi } from 'vitest'
import { Inbox, userMessage } from '../src/harness/inbox.js'
import type { HarnessRun } from '../src/harness/run.js'
import type { AssembledTurn } from '../src/sessions/turnRunner.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import type { WarmEntry } from '../src/sessions/warm.js'
import { warmAttempts } from '../src/sessions/warmTurn.js'

// warmAttempts' ends of an attempt that the cache tests cannot time
// (sessions/warmTurn.ts): a stop that lands while a streamed prompt is still
// going in, a turn stopped before anything went in, and the fallback leaving a
// live process. The process is a fake that answers only once interrupted.

const paths = { stateDir: '/nonexistent' }
const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-test' }

function assembly(): AssembledTurn {
  return {
    run: { paths },
    candidates: [],
    fingerprintParts: {
      credentialId: 'c1',
      credentialUpdatedAt: '1',
      model: null,
      ownPlugin: null,
      plugins: [],
      packageHashes: [],
      allowedPackageHashes: [],
      tiers: null,
      owner: 'user:alice',
      systemAppend: '',
      disabledTools: [],
      raisedTiers: [],
      httpRequest: false,
      toolsAsActivities: false,
      httpTier: null,
      builtinTools: [],
      recallMaxQueryChars: null,
      transientRetries: null,
      servedBuiltins: [],
      backgroundTasks: false,
      effort: null,
      agentType: null,
    },
    slot: createTurnSlot(),
    secrets: [],
    eventTierOf: () => undefined,
    shownTierOf: () => undefined,
    observe: () => Promise.resolve(),
    terminate: vi.fn(async () => {}),
    release: vi.fn(),
  }
}

/**
 * A process that, for each prompt, answers with an `error_during_execution`
 * result once interrupted (whenever that came, as Claude Code takes it), and
 * never otherwise; `close()` ends its stream.
 */
function fakeProcess(sessionId: string) {
  const inbox = new Inbox()
  let wake: (() => void) | undefined
  let interrupted = false
  let closed = false
  const interrupt = vi.fn(() => {
    interrupted = true
    wake?.()
    return Promise.resolve(undefined)
  })
  const close = vi.fn(() => {
    closed = true
    inbox.close()
    wake?.()
  })
  async function* process(): AsyncGenerator<SDKMessage, void, undefined> {
    for await (const _message of inbox) {
      while (!interrupted && !closed) await new Promise<void>((resolve) => (wake = resolve))
      if (closed) return
      interrupted = false
      yield { type: 'result', subtype: 'error_during_execution', is_error: true, total_cost_usd: 0.01, num_turns: 1, session_id: sessionId } as unknown as SDKMessage
    }
  }
  const iterator = process()
  const entry: WarmEntry = {
    sessionId,
    query: { [Symbol.asyncIterator]: () => iterator, interrupt, close },
    inbox,
    iterator,
    turn: assembly(),
    fingerprint: 'f',
    capUsd: 1,
    costAtSpawnUsd: 0,
    lastTotalUsd: 0,
    lastUsedAt: 0,
  }
  return { entry, interrupt }
}

/** The cache as warmAttempts sees it; `abandon` closes the process, as the cache's does. */
function cacheOf(entry: WarmEntry) {
  return {
    acquire: vi.fn(() => Promise.resolve({ entry, outcome: 'miss' as const })),
    settle: vi.fn(() => Promise.resolve()),
    abandon: vi.fn(() => {
      entry.query.close()
      return Promise.resolve()
    }),
    leave: vi.fn(),
  }
}

async function collect(messages: AsyncIterable<SDKMessage>): Promise<SDKMessage[]> {
  const out: SDKMessage[] = []
  for await (const m of messages) out.push(m)
  return out
}

describe('warmAttempts', () => {
  it('hears a stop that lands while a streamed prompt is going in: the process is interrupted and answers', async () => {
    const { entry, interrupt } = fakeProcess('s1')
    const warm = cacheOf(entry)
    const stop = new AbortController()
    // The prompt with its images (sessions/images.ts): the stop lands between its messages.
    async function* prompt(): AsyncGenerator<SDKUserMessage> {
      yield userMessage('look at this')
      stop.abort(new Error('interrupted by You'))
      yield userMessage([{ type: 'text', text: 'and this' }])
    }
    const attempts = warmAttempts({
      warm,
      sessionId: 's1',
      first: assembly(),
      assemble: () => Promise.reject(new Error('not expected')),
      stop: stop.signal,
      acquired: () => {},
      trace: { warm: (acquire) => acquire() },
    })
    const attempt: HarnessRun = { paths, credential, prompt: prompt(), signal: stop.signal }
    const messages = await collect(attempts.run(attempt))
    expect(messages.map((m) => m.type)).toEqual(['result'])
    expect(interrupt).toHaveBeenCalledTimes(1)
    expect(warm.settle).toHaveBeenCalledTimes(1)
    expect(warm.abandon).not.toHaveBeenCalled()
    expect(attempts.spentUsd()).toBeCloseTo(0.01, 10)
  })

  it('a turn stopped before its prompt went in leaves the process as it found it', async () => {
    const { entry, interrupt } = fakeProcess('s2')
    const warm = cacheOf(entry)
    const stop = new AbortController()
    stop.abort(new Error('interrupted by You'))
    const attempts = warmAttempts({
      warm,
      sessionId: 's2',
      first: assembly(),
      assemble: () => Promise.reject(new Error('not expected')),
      stop: stop.signal,
      acquired: () => {},
      trace: { warm: (acquire) => acquire() },
    })
    const messages = await collect(attempts.run({ paths, credential, prompt: 'hello', signal: stop.signal }))
    expect(messages).toEqual([])
    expect(warm.leave).toHaveBeenCalledWith(entry)
    expect(warm.abandon).not.toHaveBeenCalled()
    expect(interrupt).not.toHaveBeenCalled()
    expect(entry.turn.release).not.toHaveBeenCalled()
  })

  it('the fallback leaving a live process for another credential lets it end on its own', async () => {
    const { entry, interrupt } = fakeProcess('s3')
    const warm = cacheOf(entry)
    const stop = new AbortController()
    const attemptStop = new AbortController()
    const attempts = warmAttempts({
      warm,
      sessionId: 's3',
      first: assembly(),
      assemble: () => Promise.reject(new Error('not expected')),
      stop: stop.signal,
      acquired: () => {},
      trace: { warm: (acquire) => acquire() },
    })
    const running = attempts.run({ paths, credential, prompt: 'hello', signal: attemptStop.signal })[Symbol.asyncIterator]()
    const first = running.next()
    await new Promise((r) => setTimeout(r, 20))
    // fallback.ts aborts the attempt it leaves and returns the iterator.
    attemptStop.abort(new Error('the credential was refused'))
    await running.return?.()
    await first.catch(() => {})
    expect(warm.abandon).toHaveBeenCalledWith(entry, 'credential', 'drain')
    expect(interrupt).not.toHaveBeenCalled()
  })
})
