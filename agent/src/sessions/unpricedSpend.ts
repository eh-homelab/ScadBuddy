import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'

// What a turn spent on model requests Claude Code never priced (#991).
//
// Claude Code prices a request once its stream completes. A request cut off
// mid-stream is never priced: measured on SDK 0.3.283 against the fake endpoint
// (test/sessions.e2e.test.ts), a query stopped while the model streamed ends
// with an `aborted_streaming` result whose `total_cost_usd` is 0, although
// message_start had reported the request's input tokens; and the transcript's
// `cost-state` leaves it out, so the next resumed query's total does too. The
// endpoint still bills it. So the manager prices it here, from the stream it
// already sees (`includePartialMessages`), and charges it to the session.
//
// Input and cache tokens are exact (message_start reports them). Output is
// exact only when a message_delta carried it; otherwise it is estimated from
// the text streamed so far at 4 characters a token. Thinking whose text is not
// shown (omitted or redacted), signatures and citations are not counted, so
// the estimate errs low.

/** USD per million tokens. A cache write costs 1.25× input (5-minute TTL) or 2× (1-hour). */
type Price = { input: number; output: number; cacheRead: number }

/**
 * Anthropic first-party prices, by model id prefix; the first match wins, so a
 * longer id comes before its prefix. From the Claude API pricing (2026-09-25).
 */
const PRICES: readonly (readonly [string, Price])[] = [
  ['claude-fable-5-1', { input: 10, output: 50, cacheRead: 0.25 }],
  ['claude-mythos-5-1', { input: 10, output: 50, cacheRead: 0.25 }],
  ['claude-fable-5', { input: 10, output: 50, cacheRead: 1 }],
  ['claude-mythos-5', { input: 10, output: 50, cacheRead: 1 }],
  ['claude-opus-5-5', { input: 4, output: 20, cacheRead: 0.2 }],
  ['claude-opus-5', { input: 5, output: 25, cacheRead: 0.5 }],
  ['claude-opus-4-8', { input: 5, output: 25, cacheRead: 0.5 }],
  ['claude-opus-4-7', { input: 5, output: 25, cacheRead: 0.5 }],
  ['claude-opus-4-6', { input: 5, output: 25, cacheRead: 0.5 }],
  ['claude-opus-4-5', { input: 5, output: 25, cacheRead: 0.5 }],
  ['claude-opus-4', { input: 15, output: 75, cacheRead: 1.5 }],
  ['claude-3-opus', { input: 15, output: 75, cacheRead: 1.5 }],
  ['claude-sonnet-5', { input: 2, output: 10, cacheRead: 0.2 }],
  ['claude-sonnet-4', { input: 3, output: 15, cacheRead: 0.3 }],
  ['claude-3-7-sonnet', { input: 3, output: 15, cacheRead: 0.3 }],
  ['claude-haiku-4', { input: 1, output: 5, cacheRead: 0.1 }],
  ['claude-3-5-haiku', { input: 0.8, output: 4, cacheRead: 0.08 }],
]

/** By family, for an id the table does not know (a newer release, a gateway's name). */
const FAMILIES: readonly (readonly [RegExp, string])[] = [
  [/fable|mythos/, 'claude-fable-5-1'],
  [/opus/, 'claude-opus-5-5'],
  [/sonnet/, 'claude-sonnet-5'],
  [/haiku/, 'claude-haiku-4'],
]

/** The most expensive, for a model of no known family: the budget is a cap, so it is not undercharged. */
const UNKNOWN = 'claude-fable-5-1'

const lookup = (id: string): Price | undefined => PRICES.find(([prefix]) => id.startsWith(prefix))?.[1]

export function priceOf(model: string | undefined): Price {
  // `anthropic.claude-…` (Bedrock), `claude-…[1m]`, any case.
  const id = (model ?? '').toLowerCase().replace(/^.*?(?=claude-)/, '')
  const family = FAMILIES.find(([pattern]) => pattern.test(id))?.[1]
  return (lookup(id) ?? lookup(family ?? UNKNOWN) ?? lookup(UNKNOWN)) as Price
}

export type Usage = {
  input_tokens?: number | null
  output_tokens?: number | null
  cache_creation_input_tokens?: number | null
  cache_read_input_tokens?: number | null
  cache_creation?: { ephemeral_1h_input_tokens?: number | null } | null
}

const n = (v: number | null | undefined): number => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0)

/** What one request's usage costs at `model`'s price, in USD. */
export function costOf(model: string | undefined, usage: Usage): number {
  const price = priceOf(model)
  const written = n(usage.cache_creation_input_tokens)
  const hour = Math.min(n(usage.cache_creation?.ephemeral_1h_input_tokens), written)
  const tokens =
    n(usage.input_tokens) * price.input +
    n(usage.output_tokens) * price.output +
    n(usage.cache_read_input_tokens) * price.cacheRead +
    (written - hour) * price.input * 1.25 +
    hour * price.input * 2
  return tokens / 1_000_000
}

const CHARS_PER_TOKEN = 4

type Open = { model: string | undefined; usage: Usage; reportedOutput: boolean; chars: number }

/**
 * Watches a turn's SDK messages and prices the requests that started and never
 * finished: a message_start with no message_stop. A completed request is
 * Claude Code's to price, so it is dropped here, and nothing is counted twice.
 */
export class UnpricedSpend {
  /** The request each stream (the main one, or a subagent's) has open. */
  readonly #open = new Map<string, Open>()

  observe(message: SDKMessage): void {
    if (message.type !== 'stream_event') return
    const key = message.parent_tool_use_id ?? ''
    const event = message.event as {
      type: string
      message?: { model?: string; usage?: Usage }
      usage?: Usage
      delta?: { text?: string; partial_json?: string; thinking?: string }
    }
    switch (event.type) {
      case 'message_start':
        // A retry starts afresh; the request it replaces was refused or failed, not billed as streamed.
        this.#open.set(key, {
          model: event.message?.model,
          usage: { ...event.message?.usage },
          reportedOutput: false,
          chars: 0,
        })
        return
      case 'content_block_delta': {
        const open = this.#open.get(key)
        const d = event.delta
        if (open && d) open.chars += (d.text ?? d.partial_json ?? d.thinking ?? '').length
        return
      }
      case 'message_delta': {
        const open = this.#open.get(key)
        if (!open || !event.usage) return
        // message_delta's counts are cumulative for the request.
        for (const [k, v] of Object.entries(event.usage) as [keyof Usage, unknown][]) {
          if (typeof v === 'number') (open.usage as Record<string, unknown>)[k] = v
        }
        if (typeof event.usage.output_tokens === 'number') open.reportedOutput = true
        return
      }
      case 'message_stop':
        this.#open.delete(key)
        return
      default:
        return
    }
  }

  /** What the requests still open cost, in USD: 0 when every request finished. */
  usd(): number {
    let total = 0
    for (const open of this.#open.values()) {
      const output = open.reportedOutput
        ? n(open.usage.output_tokens)
        : Math.max(n(open.usage.output_tokens), Math.ceil(open.chars / CHARS_PER_TOKEN))
      total += costOf(open.model, { ...open.usage, output_tokens: output })
    }
    return total
  }
}
