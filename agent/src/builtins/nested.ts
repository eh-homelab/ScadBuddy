import { isDeepStrictEqual } from 'node:util'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { Credential } from '../credentials.js'
import { type CredentialSource, type FallbackOptions, NoUsableCredentialError, runWithFallback } from '../harness/fallback.js'
import type { HarnessPaths } from '../harness/options.js'
import { type HarnessRun, runHarness } from '../harness/run.js'
import type { BuiltinResult } from './files.js'
import type { NestedQueries, WebSearchInput } from './web.js'

// The model work WebFetch and WebSearch need in a durable turn (#2393), as one-shot Claude
// Code queries: a durable turn's on the usable credentials, tried in order as a turn tries
// them (`credentials`, runWithFallback, #3200); a classic turn's WebFetch on the turn's own
// (`credential`). Claude Code makes these calls itself in a classic turn, on the same
// credential, and counts them in the turn's cost; here each query's cost is added to the
// session's (`spent`), which its budget reads, and none may spend more than the budget
// has left (`remaining`, #3200).

/** Claude Code's small fast model, which its WebFetch answers with. */
export const SMALL_MODEL = 'haiku'
const ASK_BUDGET_USD = 0.5
const SEARCH_BUDGET_USD = 1

export type NestedDeps = {
  paths: HarnessPaths
  /** The credential a query runs on: the one the turn is on when it asks (#2973). */
  credential?: () => Promise<Credential>
  /** Or the credentials it runs on: the usable ones, in order, falling back as a turn does (#3200). */
  credentials?: CredentialSource
  /** The session the credential audit rows name. */
  sessionId?: string | undefined
  /** Adds a query's cost to the session it ran for. */
  spent?: ((costUsd: number) => Promise<void>) | undefined
  /**
   * What the session's budget has left: each query's cap is at most that, and none runs at
   * 0. An upper bound: the row's cost does not yet hold the running turn's (or segment's)
   * own model spend, which is written as it ends, so a query may still pass the budget by
   * that much; the turn's own cap and the next turn's check hold the rest.
   */
  remaining?: (() => Promise<number>) | undefined
  /** Tests: runs one query (runHarness), and asks about a refused credential (probeCredential). */
  run?: FallbackOptions['run']
  probe?: FallbackOptions['probe']
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

/** The session's budget is spent: a nested query would spend past it. */
export class BudgetSpentError extends Error {
  override name = 'BudgetSpentError'
}

/**
 * One query, on `credential` or on `credentials` in order, capped at `cap` or what the
 * session's budget has left if less; throws NoUsableCredentialError when no credential
 * is usable, BudgetSpentError when nothing is left.
 */
async function query(deps: NestedDeps, cap: number, base: Omit<HarnessRun, 'credential' | 'maxBudgetUsd'>): Promise<AsyncIterable<SDKMessage>> {
  const left = deps.remaining ? await deps.remaining() : cap
  if (!(left > 0)) throw new BudgetSpentError("the session's budget is spent")
  const capped = { ...base, maxBudgetUsd: Math.min(cap, left) }
  if (!deps.credentials) {
    if (!deps.credential) throw new NoUsableCredentialError('no Claude credential is given to this query', undefined)
    return (deps.run ?? runHarness)({ ...capped, credential: await deps.credential() })
  }
  return runWithFallback(capped, {
    candidates: await deps.credentials.candidates(),
    report: deps.credentials.reporter(deps.sessionId ? { sessionId: deps.sessionId } : {}),
    ...(deps.run ? { run: deps.run } : {}),
    ...(deps.probe ? { probe: deps.probe } : {}),
  })
}

export function nestedQueries(deps: NestedDeps): NestedQueries {
  return {
    async ask(prompt, signal) {
      const run = await query(deps, ASK_BUDGET_USD, {
        paths: deps.paths,
        prompt,
        model: SMALL_MODEL,
        maxTurns: 1,
        signal,
      })
      // Read to the end, past the result: the fallback records what the credential did after it (#3200).
      let result: Extract<SDKMessage, { type: 'result' }> | undefined
      for await (const message of run) {
        await costOf(message, deps)
        if (message.type === 'result') result ??= message
      }
      if (!result) throw new Error('Claude Code ended without an answer')
      if (result.subtype === 'success' && !result.is_error) return result.result
      throw new Error(result.subtype === 'success' ? result.result : `${result.subtype}: ${result.errors.join('; ')}`)
    },

    async search(input: WebSearchInput, signal) {
      const controller = new AbortController()
      const stop = () => controller.abort(signal.reason)
      signal.addEventListener('abort', stop, { once: true })
      // Only the exact search asked for is allowed: the query's model passes the input on.
      const same = (given: unknown) => isDeepStrictEqual({ ...(given as object) }, { ...input })
      let run: AsyncIterable<SDKMessage>
      try {
        run = await query(deps, SEARCH_BUDGET_USD, {
          paths: deps.paths,
          prompt:
            'Call the WebSearch tool exactly once, with exactly this input and nothing else, then stop:\n' +
            JSON.stringify(input),
          model: SMALL_MODEL,
          maxTurns: 2,
          signal: controller.signal,
          nestedTools: ['WebSearch'],
          tierOf: (name, given) => (name === 'WebSearch' && same(given) ? 'read' : undefined),
        })
      } catch (err) {
        signal.removeEventListener('abort', stop)
        if (err instanceof NoUsableCredentialError || err instanceof BudgetSpentError) return { text: `WebSearch cannot run: ${err.message}`, isError: true }
        throw err
      }
      let result: BuiltinResult | undefined
      try {
        for await (const message of run) {
          await costOf(message, deps)
          if (message.type === 'user' && Array.isArray(message.message.content)) {
            for (const block of message.message.content as ToolResultBlock[]) {
              if (block.type === 'tool_result') result = { text: textOf(block.content), ...(block.is_error ? { isError: true } : {}) }
            }
          }
        }
      } finally {
        signal.removeEventListener('abort', stop)
        controller.abort()
      }
      return result ?? { text: 'The web search did not run: the search model made no WebSearch call.', isError: true }
    },
  }
}
