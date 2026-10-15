import { fixedCredentials } from './fixedCredentials.js'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { Query, SDKMessage, SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'
import { Inbox, userMessage } from '../../src/harness/inbox.js'
import type { HarnessRun } from '../../src/harness/run.js'
import { SessionManager, type SessionManagerDeps } from '../../src/sessions/manager.js'
import type { Owner } from '../../src/sessions/protocol.js'
import { testPayloadKeys } from './payloadKeys.js'

export const browser: Owner = { kind: 'browser', id: 'browser', label: 'You' }
export const agentA: Owner = { kind: 'bearer', id: 'token:a', label: 'Agent A' }
export const agentB: Owner = { kind: 'bearer', id: 'token:b', label: 'Agent B' }

/** What a scripted stand-in for the SDK does for one turn. */
export type FakeTurn =
  | {
      reply: string
      /**
       * The result's `total_cost_usd`: the PROCESS's running total, as Claude
       * Code reports it (spec 2026-10-13 §2.2), so a second turn on the same
       * process names what both turns cost together. One cent more than the
       * last when omitted.
       */
      costUsd?: number
      /** The result's subtype; `success` when omitted. */
      subtype?: 'success' | 'error_max_budget_usd' | 'error_max_turns'
      /** Keeps the stream open after the result until this settles (the SDK's last appends). */
      holdAfterResult?: Promise<void>
      /** SDK messages yielded before the reply, e.g. a tool call and its result (#782). */
      before?: SDKMessage[]
    }
  /**
   * Waits for the interrupt, then ends the turn as Claude Code does: with an
   * `error_during_execution` result, carrying `resultOnAbortUsd` as the total
   * when given, else the total so far (harness/run.ts stopFirst, #1168). The
   * process stays for the next turn.
   */
  | { hang: true; resultOnAbortUsd?: number }
  /**
   * Starts a reply (message_start with this usage, then `text`) and waits for
   * the interrupt, as a model cut off mid-reply (#991). Then ends with the
   * interrupt's result, whose total is `resultCostUsd` (Claude Code prices
   * nothing for the cut-off request), or throws (the process dies) when that
   * is undefined.
   */
  | {
      stall: { model: string; usage: Record<string, number>; text?: string }
      resultCostUsd?: number
      /** Dies with this error right after the text, instead of waiting for an interrupt. */
      dies?: string
    }
  | { throws: string }

/** What the scripted process's stream also offers, as the SDK's Query does (sessions/warm.ts WarmQuery). */
export type ScriptedStream = AsyncIterable<SDKMessage> & Pick<Query, 'interrupt' | 'close'>

/**
 * A QueryRunner that yields SDK-shaped messages without the SDK, for the
 * manager's own logic (claims, ownership, interrupt). One call is one
 * process (`runs`), as the warm cache spawns it (sessions/warm.ts): its
 * `prompt` is the process's inbox, and `next` is asked for each user message
 * pushed into it (`prompts` keeps them), with that message's text, so one
 * process serves turn after turn until the cache closes it. Choose a turn by
 * that text, never by `run.prompt`: an inbox, not the text (#3667). `interrupt()` ends the turn in progress as
 * Claude Code's does; `close()` ends the stream. Wrapped in a plain generator
 * (`yield* scripted.runner(run)`), those are gone, and the manager's abort
 * signal stands in for them. The real SDK is covered by test/sessions.e2e.test.ts.
 */
/**
 * `loads`: a resumed process loads its transcript through `run.sessionStore` first, as the SDK
 * does (sdk.mjs: `sessionStore.load({projectKey, sessionId: resume})` before Claude Code starts);
 * false for one that carries nothing in. Its totals are still the script's (`costUsd`).
 */
export function scriptedRunner(next: (run: HarnessRun, text: string) => FakeTurn, { loads = true }: { loads?: boolean } = {}) {
  const runs: HarnessRun[] = []
  const prompts: SDKUserMessage[] = []
  let n = 0
  const runner = (run: HarnessRun): ScriptedStream => {
    runs.push(run)
    const session_id = run.sessionId ?? run.resume ?? 'unknown'
    /** The process's cumulative `total_cost_usd`. */
    let total = 0
    /** How a turn in progress is stopped: `interrupt()`, or `close()` (the stream ends). */
    let stopped: ((how: 'interrupt' | 'close') => void) | undefined
    /** User messages read so far: the turn in progress is the `reads`-th. */
    let reads = 0
    /** Whether a turn is in progress (its message read, its result not yet out). */
    let inTurn = false
    /**
     * An interrupt no turn has asked for yet, by the turn it is for (#3661). Claude Code
     * reads stdin in order: one sent during a turn is that turn's, one sent after a message
     * was pushed but before it was read is that message's turn's, and one with neither is nothing.
     */
    let pending: number | undefined
    let closed = false
    const unread = () => (run.prompt instanceof Inbox ? run.prompt.waiting : 0)
    const stop = (how: 'interrupt' | 'close') => {
      if (how === 'close') closed = true
      const waiting = stopped
      stopped = undefined
      if (waiting) waiting(how)
      else if (how === 'interrupt') {
        if (inTurn) pending = reads
        else if (unread() > 0) pending = reads + 1
      }
    }
    const whenStopped = () => {
      if (closed) return Promise.resolve('close' as const)
      if (pending === reads) {
        pending = undefined
        return Promise.resolve('interrupt' as const)
      }
      return new Promise<'interrupt' | 'close'>((resolve) => (stopped = resolve))
    }
    run.signal?.addEventListener('abort', () => stop(abortedBy(run.signal?.reason)), { once: true })
    const stream = (event: Record<string, unknown>) =>
      ({ type: 'stream_event', event, parent_tool_use_id: null, uuid: `u-${Math.random()}`, session_id }) as unknown as SDKMessage
    const result = (fields: Record<string, unknown>) => ({ type: 'result', total_cost_usd: total, session_id, ...fields }) as unknown as SDKMessage
    async function* process(): AsyncGenerator<SDKMessage, void, undefined> {
      await Promise.resolve()
      if (loads && run.resume && run.sessionStore) await run.sessionStore.load({ projectKey: '', sessionId: run.resume })
      const inbox = typeof run.prompt === 'string' ? [userMessage(run.prompt)] : run.prompt
      for await (const message of inbox) {
        if (closed) return
        reads++
        inTurn = true
        // One for a turn that ended without asking for it is gone with that turn.
        if (pending !== undefined && pending < reads) pending = undefined
        prompts.push(message)
        const turn = next(run, promptText(message))
        const msgId = `msg_fake_${++n}`
        // The memory hooks, as Claude Code fires them: recall at the prompt, retain at Stop (memory/hindsight.ts).
        const hook = { session_id, transcript_path: '/nonexistent', cwd: '/' }
        const hookOptions = { signal: new AbortController().signal }
        for (const matcher of run.memoryHooks?.UserPromptSubmit ?? []) {
          for (const h of matcher.hooks) await h({ ...hook, hook_event_name: 'UserPromptSubmit', prompt: promptText(message) }, undefined, hookOptions)
        }
        if ('hang' in turn) {
          if ((await whenStopped()) === 'close') return
          if (turn.resultOnAbortUsd !== undefined) total = turn.resultOnAbortUsd
          inTurn = false
          yield result({ subtype: 'error_during_execution', is_error: true, errors: [], num_turns: 1 })
          continue
        }
        if ('throws' in turn) throw new Error(turn.throws)
        if ('stall' in turn) {
          const { model, usage, text = '' } = turn.stall
          yield stream({ type: 'message_start', message: { id: msgId, model, usage } })
          yield stream({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } })
          yield stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text } })
          if (turn.dies !== undefined) throw new Error(turn.dies)
          if ((await whenStopped()) === 'close') return
          if (turn.resultCostUsd === undefined) throw new Error('Claude Code process aborted by user')
          total = turn.resultCostUsd
          inTurn = false
          yield result({ subtype: 'error_during_execution', is_error: true, errors: [], num_turns: 0, terminal_reason: 'aborted_streaming' })
          continue
        }
        for (const m of turn.before ?? []) yield m
        yield stream({ type: 'message_start', message: { id: msgId } })
        yield stream({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } })
        yield stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: turn.reply } })
        yield {
          type: 'assistant',
          message: { id: msgId, content: [{ type: 'text', text: turn.reply }] },
          parent_tool_use_id: null,
          session_id,
        } as unknown as SDKMessage
        yield stream({ type: 'content_block_stop', index: 0 })
        for (const matcher of run.memoryHooks?.Stop ?? []) {
          for (const h of matcher.hooks) {
            await h({ ...hook, hook_event_name: 'Stop', stop_hook_active: false, last_assistant_message: turn.reply }, undefined, hookOptions)
          }
        }
        const subtype = turn.subtype ?? 'success'
        total = turn.costUsd ?? total + 0.01
        inTurn = false
        yield result({
          subtype,
          is_error: subtype !== 'success',
          ...(subtype !== 'success' ? { errors: [`Reached maximum budget ($0.025546000000000007)`] } : {}),
          num_turns: 1,
        })
        await turn.holdAfterResult
      }
      // What Claude Code writes as it exits on stdin EOF (measured, sessions/warm.ts
      // DRAIN_MS): the last prompt and its running total, for the session's next
      // process to carry in (`costStateUsd`). Not after `close()`, as the real one's are lost.
      if (!closed) {
        await run.sessionStore?.append({ projectKey: '', sessionId: session_id }, [{ type: 'last-prompt' }, { type: 'cost-state', totalCostUSD: total }])
      }
    }
    const iterator = process()
    return {
      [Symbol.asyncIterator]: () => iterator,
      interrupt: () => {
        stop('interrupt')
        return Promise.resolve(undefined)
      },
      close: () => {
        stop('close')
        void iterator.return().catch(() => {})
      },
    }
  }
  return { runner, runs, prompts }
}

/** What the manager's abort of a wrapped stream means (manager.ts `startedQuery`): a close, or an interrupt. */
function abortedBy(reason: unknown): 'interrupt' | 'close' {
  return reason instanceof Error && reason.message === 'the process was closed' ? 'close' : 'interrupt'
}

/** The text of a pushed user message (`scriptedRunner().prompts`): its text blocks joined, or the string it is. */
export function promptText(message: SDKUserMessage): string {
  const content = message.message.content
  if (typeof content === 'string') return content
  return content.flatMap((block) => (block.type === 'text' ? [block.text] : [])).join('')
}

export async function tempPaths(): Promise<{ stateDir: string }> {
  return { stateDir: await mkdtemp(path.join(os.tmpdir(), 'sessions-')) }
}

export function manager(deps: Partial<SessionManagerDeps> & Pick<SessionManagerDeps, 'sql' | 'paths'>): SessionManager {
  return new SessionManager({
    credentials: fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' }),
    renewMs: 50,
    pollMs: 50,
    payloadKeys: testPayloadKeys(deps.sql),
    ...deps,
  })
}

/** Collects attach events until `until` matches one (inclusive), or the timeout. */
export async function collectUntil<T extends { event: { type: string } }>(
  events: AsyncIterable<T>,
  until: (e: T) => boolean,
  timeoutMs = 10_000,
): Promise<T[]> {
  const out: T[] = []
  let timer: NodeJS.Timeout | undefined
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timed out; got ${out.map((e) => e.event.type).join(', ')}`)), timeoutMs)
  })
  const read = (async () => {
    for await (const e of events) {
      out.push(e)
      if (until(e)) return out
    }
    return out
  })()
  try {
    return await Promise.race([read, timeout])
  } finally {
    clearTimeout(timer)
  }
}
