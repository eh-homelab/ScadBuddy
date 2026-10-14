import { isDeepStrictEqual } from 'node:util'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { Credential } from '../credentials.js'
import type { HarnessPaths } from '../harness/options.js'
import { runHarness } from '../harness/run.js'
import type { BuiltinResult } from './files.js'
import type { NestedQueries, WebSearchInput } from './web.js'

// The model work WebFetch and WebSearch need in a durable turn (#2393), as one-shot Claude
// Code queries on the session's credential. Claude Code makes these calls itself in a
// classic turn, on the same credential, and counts them in the turn's cost; here each
// query's cost is added to the session's (`spent`), which its budget reads.

/** Claude Code's small fast model, which its WebFetch answers with. */
export const SMALL_MODEL = 'haiku'
const ASK_BUDGET_USD = 0.5
const SEARCH_BUDGET_USD = 1

export type NestedDeps = {
  paths: HarnessPaths
  /** The credential a query runs on (the first usable one, as a turn starts on). */
  credential: () => Promise<Credential>
  /** Adds a query's cost to the session it ran for. */
  spent?: ((costUsd: number) => Promise<void>) | undefined
}

type ToolResultBlock = { type: string; tool_use_id?: string; content?: unknown; is_error?: boolean }

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return (content as { type?: string; text?: string }[]).map((b) => (b.type === 'text' ? (b.text ?? '') : '')).join('')
}

async function costOf(message: SDKMessage, deps: NestedDeps): Promise<void> {
  if (message.type === 'result' && message.total_cost_usd > 0) await deps.spent?.(message.total_cost_usd)
}

export function nestedQueries(deps: NestedDeps): NestedQueries {
  return {
    async ask(prompt, signal) {
      const run = runHarness({
        paths: deps.paths,
        credential: await deps.credential(),
        prompt,
        model: SMALL_MODEL,
        maxTurns: 1,
        maxBudgetUsd: ASK_BUDGET_USD,
        signal,
      })
      for await (const message of run) {
        await costOf(message, deps)
        if (message.type !== 'result') continue
        if (message.subtype === 'success' && !message.is_error) return message.result
        throw new Error(message.subtype === 'success' ? message.result : `${message.subtype}: ${message.errors.join('; ')}`)
      }
      throw new Error('Claude Code ended without an answer')
    },

    async search(input: WebSearchInput, signal) {
      const controller = new AbortController()
      const stop = () => controller.abort(signal.reason)
      signal.addEventListener('abort', stop, { once: true })
      // Only the exact search asked for is allowed: the query's model passes the input on.
      const same = (given: unknown) => isDeepStrictEqual({ ...(given as object) }, { ...input })
      const run = runHarness({
        paths: deps.paths,
        credential: await deps.credential(),
        prompt:
          'Call the WebSearch tool exactly once, with exactly this input and nothing else, then stop:\n' +
          JSON.stringify(input),
        model: SMALL_MODEL,
        maxTurns: 2,
        maxBudgetUsd: SEARCH_BUDGET_USD,
        signal: controller.signal,
        nestedTools: ['WebSearch'],
        tierOf: (name, given) => (name === 'WebSearch' && same(given) ? 'read' : undefined),
      })
      let result: BuiltinResult | undefined
      try {
        for await (const message of run) {
          await costOf(message, deps)
          if (message.type === 'user' && Array.isArray(message.message.content)) {
            for (const block of message.message.content as ToolResultBlock[]) {
              if (block.type === 'tool_result') result = { text: textOf(block.content), ...(block.is_error ? { isError: true } : {}) }
            }
          }
          if (message.type === 'result') break
        }
      } finally {
        signal.removeEventListener('abort', stop)
        controller.abort()
      }
      return result ?? { text: 'The web search did not run: the search model made no WebSearch call.', isError: true }
    },
  }
}
