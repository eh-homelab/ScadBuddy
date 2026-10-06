import type * as Sdk from '@anthropic-ai/claude-agent-sdk'
import type { Options, SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// The SDK's Query (0.3.283) delegates next/return/throw to an inner stream
// and returns that inner stream from [Symbol.asyncIterator](), so a for-await
// loop over the Query never calls the Query's own methods (#1009). This fake
// has the same shape, and writes a stderr line with no trailing newline.
const fake = vi.hoisted(() => ({ stderrChunk: '' }))

vi.mock('@anthropic-ai/claude-agent-sdk', async (importOriginal) => {
  const actual = await importOriginal<typeof Sdk>()
  class FakeQuery {
    private readonly inner: AsyncGenerator<SDKMessage, void>
    constructor(options: Options) {
      this.inner = (async function* () {
        options.stderr?.(fake.stderrChunk)
        yield { type: 'system' } as unknown as SDKMessage
        yield { type: 'system' } as unknown as SDKMessage
      })()
    }
    next(...args: [] | [unknown]) {
      return this.inner.next(...args)
    }
    return(value?: unknown) {
      return this.inner.return(value as undefined)
    }
    throw(err: unknown) {
      return this.inner.throw(err)
    }
    [Symbol.asyncIterator]() {
      return this.inner
    }
    interrupt() {
      return Promise.resolve()
    }
  }
  return { ...actual, query: ({ options }: { options: Options }) => new FakeQuery(options) }
})

const { runHarness } = await import('../src/harness/run.js')

const API_KEY = 'sk-ant-api03-unit-test-key-000011112222'

describe('runHarness under for-await (#1009)', () => {
  const run = (stderr: string[]) => ({
    paths: { stateDir: '/var/lib/scadbuddy-agent' },
    credential: { kind: 'anthropic_api_key' as const, secret: API_KEY },
    prompt: 'hi',
    stderr: (l: string) => stderr.push(l),
  })

  beforeEach(() => {
    fake.stderrChunk = `last line with ${API_KEY}`
  })

  it('flushes a trailing partial stderr line, redacted, when the stream ends', async () => {
    const stderr: string[] = []
    for await (const _ of runHarness(run(stderr))) {
      // drain
    }
    expect(stderr).toHaveLength(1)
    expect(stderr[0]).toContain('last line with')
    expect(stderr[0]).not.toContain(API_KEY)
  })

  it('flushes it when the loop breaks early', async () => {
    const stderr: string[] = []
    for await (const _ of runHarness(run(stderr))) break
    expect(stderr).toHaveLength(1)
    expect(stderr[0]).not.toContain(API_KEY)
  })
})
