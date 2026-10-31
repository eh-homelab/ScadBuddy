import { fixedCredentials } from './fixedCredentials.js'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { Query, SDKMessage, SDKUserMessage, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { Inbox, userMessage } from '../../src/harness/inbox.js'
import type { HarnessRun } from '../../src/harness/run.js'
import { SessionManager, type SessionManagerDeps } from '../../src/sessions/manager.js'
import type { Owner } from '../../src/sessions/protocol.js'
import { testPayloadKeys } from './payloadKeys.js'
import { serveManager } from './agentSessions.js'

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

/** A `cost-state` transcript entry as Claude Code writes one (every field its schema requires), carrying `totalCostUSD`. */
export function costStateEntry(sessionId: string, totalCostUSD: number): SessionStoreEntry {
  return {
    type: 'cost-state',
    sessionId,
    totalCostUSD,
    totalAPIDuration: 0,
    totalAPIDurationWithoutRetries: 0,
    totalToolDuration: 0,
    totalLinesAdded: 0,
    totalLinesRemoved: 0,
    totalDuration: 0,
    startTime: 0,
    modelUsage: {},
  }
}

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
 * false for one that carries nothing in, which the real SDK never is. `beforeLoad` runs after
 * the spawn and just before that load: a write that lands in between. Its totals are still the
 * script's (`costUsd`).
 */
export function scriptedRunner(
  next: (run: HarnessRun, text: string) => FakeTurn,
  { loads = true, beforeLoad }: { loads?: boolean; beforeLoad?: () => Promise<void> } = {},
) {
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
      // A resume restores the running total from the transcript the SDK loads
      // through the store before Claude Code starts: its last `cost-state` of
      // this session (test/warmResumeCost.sdk.test.ts measures the real rule).
      if (loads && run.resume !== undefined && run.sessionStore) {
        await beforeLoad?.()
        const loaded = (await run.sessionStore.load({ projectKey: '', sessionId: session_id })) ?? []
        const state = loaded.findLast((e) => e.type === 'cost-state' && e.sessionId === session_id)
        if (typeof state?.totalCostUSD === 'number') total = state.totalCostUSD
      }
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
      // process to carry in (warm.ts `restoredCostUsd`). Not after `close()`, as the real one's are lost.
      if (!closed) {
        await run.sessionStore?.append({ projectKey: '', sessionId: session_id }, [{ type: 'last-prompt' }, costStateEntry(session_id, total)])
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

/** Every manager `manager()` made that `stopManagers()` has not stopped yet. */
const unstopped = new Set<SessionManager>()

/**
 * A manager over the test's database. In a describe that called `agentSessions()`
 * (support/agentSessions.ts) its sessions run on AgentSession on that file's Temporal dev
 * server, unless `classic` keeps it to the in-process turn (a test of that turn's own lease,
 * reaper or approvals).
 */
export function manager(
  deps: Partial<SessionManagerDeps> & Pick<SessionManagerDeps, 'sql' | 'paths'>,
  options: { classic?: boolean } = {},
): SessionManager {
  const m = new SessionManager({
    credentials: fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' }),
    renewMs: 50,
    pollMs: 50,
    payloadKeys: testPayloadKeys(deps.sql),
    ...deps,
  })
  if (!options.classic) serveManager(m, deps)
  unstopped.add(m)
  return m
}

/**
 * Stops every manager `manager()` made since the last call, as a shutdown does
 * (`stopTurns`): its turns aborted, then its warm Claude Code processes closed.
 * A test on the real SDK calls it in its teardown, before it closes the fake
 * Anthropic and the pool the turns finish in. A process left running (a turn
 * parked on an approval, an idle warm one) outlived its test, kept its turn
 * going once the test process was gone, and sent Messages API calls to
 * whatever server the OS gave the fake's port to next (#5030).
 */
export async function stopManagers(): Promise<void> {
  const managers = [...unstopped]
  unstopped.clear()
  // The managers stop together, so this takes about 7 s at most whatever their number
  // (5 s for the aborted turns, then up to sessions/warm.ts DRAIN_MS for the warm
  // processes): inside the 20 s `hookTimeout` (vitest.config.ts) of the afterEach that
  // calls it. The leak guard's wait (leakGuard.ts EXIT_WAIT_MS) runs in afterAll, a hook
  // with a budget of its own.
  await Promise.all(managers.map((m) => m.stopTurns({ graceMs: 0, abortWaitMs: 5_000 })))
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
