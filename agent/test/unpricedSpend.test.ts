import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { costOf, priceOf, UnpricedSpend } from '../src/sessions/unpricedSpend.js'

// #991: the spend of a model request cut off mid-stream, which Claude Code never prices.

const ev = (event: Record<string, unknown>, parent: string | null = null) =>
  ({ type: 'stream_event', event, parent_tool_use_id: parent, uuid: 'u', session_id: 's' }) as unknown as SDKMessage
const start = (model: string, usage: Record<string, number>, parent: string | null = null) =>
  ev({ type: 'message_start', message: { model, usage } }, parent)
const text = (t: string, parent: string | null = null) =>
  ev({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: t } }, parent)

describe('priceOf', () => {
  it.each([
    ['claude-sonnet-4-5', 3, 15],
    ['claude-sonnet-4-5-20250929', 3, 15],
    ['claude-sonnet-5-5', 2, 10],
    ['claude-opus-5-5', 4, 20],
    ['claude-opus-5', 5, 25],
    ['claude-opus-4-1', 15, 75],
    ['claude-3-opus-latest', 15, 75],
    ['claude-opus-4-6[1m]', 5, 25],
    ['anthropic.claude-haiku-4-5', 1, 5],
    ['claude-fable-5-1', 10, 50],
    // A newer release of a known family: that family's current price.
    ['claude-sonnet-9', 2, 10],
    // Nothing known: the most expensive, so a budget is never undercharged.
    ['my-gateway-model', 10, 50],
  ])('%s costs $%d / $%d per MTok', (model, input, output) => {
    expect(priceOf(model)).toMatchObject({ input, output })
  })
})

describe('costOf', () => {
  it('prices input, output, cache reads and both cache-write TTLs', () => {
    expect(
      costOf('claude-sonnet-4-5', {
        input_tokens: 1_000_000,
        output_tokens: 1_000_000,
        cache_read_input_tokens: 1_000_000,
        cache_creation_input_tokens: 2_000_000,
        cache_creation: { ephemeral_1h_input_tokens: 1_000_000 },
      }),
    ).toBeCloseTo(3 + 15 + 0.3 + 3 * 1.25 + 3 * 2, 10)
  })
})

describe('UnpricedSpend', () => {
  it('is 0 when every request finished: Claude Code priced those', () => {
    const s = new UnpricedSpend()
    s.observe(start('claude-sonnet-4-5', { input_tokens: 1000 }))
    s.observe(text('hello'))
    s.observe(ev({ type: 'message_delta', delta: {}, usage: { output_tokens: 5 } }))
    s.observe(ev({ type: 'message_stop' }))
    expect(s.usd()).toBe(0)
  })

  it('prices a request cut off mid-stream, estimating output from the streamed text', () => {
    const s = new UnpricedSpend()
    s.observe(start('claude-sonnet-4-5', { input_tokens: 100_000, output_tokens: 1 }))
    s.observe(text('x'.repeat(2000)))
    s.observe(text('y'.repeat(2000)))
    // 100k × $3 + 1000 × $15 per MTok.
    expect(s.usd()).toBeCloseTo(0.3 + 0.015, 10)
  })

  it('uses a reported output count over the estimate', () => {
    const s = new UnpricedSpend()
    s.observe(start('claude-sonnet-4-5', { input_tokens: 0 }))
    s.observe(text('x'.repeat(4000)))
    s.observe(ev({ type: 'message_delta', delta: {}, usage: { output_tokens: 10 } }))
    expect(s.usd()).toBeCloseTo((10 * 15) / 1_000_000, 12)
  })

  it('counts a subagent’s open request beside the main one, and only the open one after a completed request', () => {
    const s = new UnpricedSpend()
    s.observe(start('claude-sonnet-4-5', { input_tokens: 1_000_000 }))
    s.observe(ev({ type: 'message_stop' }))
    s.observe(start('claude-sonnet-4-5', { input_tokens: 100_000 }))
    s.observe(start('claude-haiku-4-5', { input_tokens: 100_000 }, 'toolu_1'))
    expect(s.usd()).toBeCloseTo(0.3 + 0.1, 10)
  })

  it('ignores everything but stream events', () => {
    const s = new UnpricedSpend()
    s.observe({ type: 'assistant', message: { usage: { input_tokens: 1_000_000 } } } as unknown as SDKMessage)
    expect(s.usd()).toBe(0)
  })
})
