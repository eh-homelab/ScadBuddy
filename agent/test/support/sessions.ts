import { fixedCredentials } from './fixedCredentials.js'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { HarnessRun } from '../../src/harness/run.js'
import { SessionManager, type SessionManagerDeps } from '../../src/sessions/manager.js'
import type { Owner } from '../../src/sessions/protocol.js'

export const browser: Owner = { kind: 'browser', id: 'browser', label: 'You' }
export const agentA: Owner = { kind: 'bearer', id: 'token:a', label: 'Agent A' }
export const agentB: Owner = { kind: 'bearer', id: 'token:b', label: 'Agent B' }

/** What a scripted stand-in for the SDK does for one query. */
export type FakeTurn =
  | {
      reply: string
      costUsd?: number
      /** The result's subtype; `success` when omitted. */
      subtype?: 'success' | 'error_max_budget_usd' | 'error_max_turns'
      /** Keeps the stream open after the result until this settles (the SDK's last appends). */
      holdAfterResult?: Promise<void>
    }
  /** Waits until the query is aborted, then throws as the SDK does. */
  | { hang: true }
  | { throws: string }

/**
 * A QueryRunner that yields SDK-shaped messages without the SDK, for the
 * manager's own logic (claims, ownership, interrupt). The real SDK is covered
 * by test/sessions.e2e.test.ts.
 */
export function scriptedRunner(next: (run: HarnessRun) => FakeTurn) {
  const runs: HarnessRun[] = []
  let n = 0
  const runner = (run: HarnessRun): AsyncIterable<SDKMessage> => {
    runs.push(run)
    const turn = next(run)
    const msgId = `msg_fake_${++n}`
    const session_id = run.sessionId ?? run.resume ?? 'unknown'
    const stream = (event: Record<string, unknown>) =>
      ({ type: 'stream_event', event, parent_tool_use_id: null, uuid: `u-${Math.random()}`, session_id }) as unknown as SDKMessage
    return (async function* () {
      await Promise.resolve()
      if ('hang' in turn) {
        await new Promise((_, reject) => {
          const fail = () => reject(new Error('Claude Code process aborted by user'))
          if (run.signal?.aborted) fail()
          run.signal?.addEventListener('abort', fail, { once: true })
        })
        return
      }
      if ('throws' in turn) throw new Error(turn.throws)
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
      yield {
        type: 'result',
        subtype: turn.subtype ?? 'success',
        is_error: (turn.subtype ?? 'success') !== 'success',
        ...(turn.subtype && turn.subtype !== 'success' ? { errors: [`Reached maximum budget ($0.025546000000000007)`] } : {}),
        num_turns: 1,
        total_cost_usd: turn.costUsd ?? 0.01,
        session_id,
      } as unknown as SDKMessage
      await turn.holdAfterResult
    })()
  }
  return { runner, runs }
}

export async function tempPaths(): Promise<{ stateDir: string }> {
  return { stateDir: await mkdtemp(path.join(os.tmpdir(), 'sessions-')) }
}

export function manager(deps: Partial<SessionManagerDeps> & Pick<SessionManagerDeps, 'sql' | 'paths'>): SessionManager {
  return new SessionManager({
    credentials: fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' }),
    renewMs: 50,
    pollMs: 50,
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
