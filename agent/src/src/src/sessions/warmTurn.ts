import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { type Inbox, userMessage } from '../harness/inbox.js'
import { DEFAULT_MAX_BUDGET_USD, type HarnessRun, INTERRUPT_GRACE_MS } from '../harness/run.js'
import type { MemoryHooks } from '../memory/hindsight.js'
import type { TurnTrace } from '../telemetry/turn.js'
import type { AssembledTurn } from './turnRunner.js'
import { turnCostUsd, type WarmEntry, type WarmSessions, type WarmTurn } from './warm.js'

// One classic turn on the session's warm Claude Code (unified runtime spec
// 2026-10-13 §5.3, §6.6, §6.7; plan Task 2.3). `warmAttempts` is what
// `SessionManager.runTurn` gives `runWithFallback` (harness/fallback.ts) as its
// `run`: each attempt of the turn acquires the session's process from the
// cache (warm.ts) for the attempt's credential and remaining budget, pushes the
// attempt's prompt into the process's inbox, and reads its messages up to the
// `result`, which settles the entry: the process stays for the next turn.
// Every other end abandons the entry, so the process goes and the next turn
// spawns afresh:
//
//   - the stream ended, or threw, without a result. A hit whose stream ends
//     before its first message is a process that died between turns: it is
//     abandoned and the attempt is tried once more, on a fresh spawn;
//   - the fallback left the attempt early (its signal aborted while the turn's
//     own `stop` is not): a refused credential. The next attempt's acquire
//     spawns with the next credential (§6.7);
//   - the turn's `stop` aborted (an interrupt, a lost claim, a shutdown): the
//     process is told to interrupt, as Claude Code's own Esc does, and answers
//     with an `error_during_execution` result, which settles it (harness/run.ts
//     `stopFirst` measured this on SDK 0.3.283 and 0.3.287). Only when it has
//     not answered within the grace is it abandoned. The abort signal never
//     reaches the process: a warm spawn has none (warm.ts), so an abort cannot
//     close it (§5.3 "The abort signal is not used for a warm turn").
//
// The cache takes each assembly it is given: a miss keeps it with the process,
// a hit releases it (warm.ts). So the first attempt runs on the turn's own
// assembly, and every later one (a fallback, the retry after a process had
// gone) assembles again; `unused` is the one no acquire has taken yet, the
// caller's to release if the turn ends first.
//
// The attempt's `maxRetries` (the fallback's bound on Claude Code's retries
// when there is a credential behind this one) goes in the process's env, so it
// is set on the assembly the spawn gets and fingerprinted (`transientRetries`,
// turnRunner.ts): a process is reused only with the same bound. Its `maxTurns`
// and `maxBudgetUsd` are not: the cap is the session's, fixed per process, and
// the remaining budget is the cache's own check (§5.3 "budget").

export type WarmTurnDeps = {
  warm: Pick<WarmSessions, 'acquire' | 'settle' | 'abandon' | 'leave'>
  sessionId: string
  /** The turn's assembly, for its first attempt. */
  first: AssembledTurn
  /** Assembles again for an attempt after the first. */
  assemble: () => Promise<AssembledTurn>
  /** The turn's own signal (manager.ts `LocalTurn.controller`): an interrupt, a lost claim, a shutdown. */
  stop: AbortSignal
  /** Told of each acquire before the prompt goes in: the entry's assembly is the turn's from then on (its slot, secrets, tiers). */
  acquired: (acquired: WarmTurn) => void
  trace: Pick<TurnTrace, 'warm'>
  /** How long an interrupted turn may take to end with its result before its process is abandoned (INTERRUPT_GRACE_MS). */
  interruptGraceMs?: number
}

export type WarmTurnRun = {
  /** `runWithFallback`'s `run`. */
  run: (attempt: HarnessRun) => AsyncIterable<SDKMessage>
  /** What the turn's attempts spent so far, as their results priced it (warm.ts `turnCostUsd`), summed. */
  spentUsd: () => number
  /** The assembly no acquire has taken yet: the caller's to release if the turn ends before one. */
  unused: () => AssembledTurn | undefined
}

/** How an attempt on an entry ended. */
type Served = 'settled' | 'gone' | 'ended'

export function warmAttempts(deps: WarmTurnDeps): WarmTurnRun {
  let unused: AssembledTurn | undefined = deps.first
  let spent = 0
  const graceMs = deps.interruptGraceMs ?? INTERRUPT_GRACE_MS

  /** The assembly for the next acquire: the turn's own until the cache took it, then a fresh one. */
  const assembly = async (): Promise<AssembledTurn> => {
    unused ??= await deps.assemble()
    return unused
  }

  async function* run(attempt: HarnessRun): AsyncGenerator<SDKMessage, void, undefined> {
    let retried = false
    for (;;) {
      const turn = await assembly()
      const bounded: AssembledTurn = {
        ...turn,
        run: { ...turn.run, ...(attempt.maxRetries === undefined ? {} : { maxRetries: attempt.maxRetries }) },
        fingerprintParts: { ...turn.fingerprintParts, transientRetries: attempt.maxRetries ?? null },
      }
      let acquired: WarmTurn
      try {
        acquired = await deps.trace.warm(() =>
          deps.warm.acquire(deps.sessionId, bounded, attempt.credential, attempt.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD),
        )
      } catch (err) {
        // On a throw the assembly is still ours (warm.ts `acquire`).
        unused = undefined
        turn.release()
        throw err
      }
      unused = undefined
      deps.acquired(acquired)
      const served = yield* serve(acquired.entry, attempt, acquired.outcome === 'hit' && !retried)
      if (served !== 'gone') return
      retried = true
    }
  }

  /** One attempt on the entry: the prompt in, the messages out up to the result. */
  async function* serve(entry: WarmEntry, attempt: HarnessRun, mayRetry: boolean): AsyncGenerator<SDKMessage, Served, undefined> {
    let sawMessage = false
    let settled = false
    let abandoned = false
    /** Given back untouched: the turn was stopped before its prompt went in. */
    let left = false
    let grace: ReturnType<typeof setTimeout> | undefined
    const abandon = (reason: string, how: 'close' | 'drain' = 'close') => {
      if (settled || abandoned || left) return
      abandoned = true
      clearTimeout(grace)
      void deps.warm.abandon(entry, reason, how)
    }
    /** The attempt's signal aborted: by the fallback leaving a refused credential, or through the link by the turn's own `stop`. */
    const onAbort = () => {
      if (settled || abandoned) return
      if (!deps.stop.aborted) {
        // The process is alive and well, only on the wrong credential: it may end on its own and land its appends.
        abandon('credential', 'drain')
        return
      }
      grace = setTimeout(() => abandon('the interrupt grace period ran out'), graceMs)
      grace.unref()
      entry.query.interrupt().catch(() => abandon('the interrupt failed'))
    }
    const signal = attempt.signal
    try {
      if (signal?.aborted) {
        // Nothing went in: the process is as the turn found it, and stays for the next.
        left = true
        deps.warm.leave(entry)
        return 'ended'
      }
      await push(entry.inbox, attempt.prompt)
      signal?.addEventListener('abort', onAbort, { once: true })
      // A stop that landed while the prompt was going in (a streamed one, with images) was not heard above.
      if (signal?.aborted) onAbort()
      for (;;) {
        const next = await entry.iterator.next()
        if (next.done) break
        sawMessage = true
        yield next.value
        if (next.value.type === 'result') {
          spent += turnCostUsd(entry, next.value)
          await deps.warm.settle(entry, next.value)
          settled = true
          return 'settled'
        }
      }
      if (mayRetry && !sawMessage && !abandoned) {
        abandon('the process had gone')
        return 'gone'
      }
      abandon('the stream ended without a result')
      return 'ended'
    } catch (err) {
      if (mayRetry && !sawMessage && !abandoned) {
        abandon('the process had gone')
        return 'gone'
      }
      abandon('the query threw')
      throw err
    } finally {
      clearTimeout(grace)
      signal?.removeEventListener('abort', onAbort)
      // The consumer returned early (the fallback leaving the attempt), with no result in.
      abandon('the attempt was left early')
    }
  }

  return { run, spentUsd: () => spent, unused: () => unused }
}

/** The attempt's prompt into the process: a string as one user message, a stream as each of its messages (images, sessions/images.ts). */
async function push(inbox: Inbox, prompt: HarnessRun['prompt']): Promise<void> {
  if (typeof prompt === 'string') {
    inbox.push(userMessage(prompt))
    return
  }
  for await (const message of prompt) inbox.push(message)
}

/**
 * A continuation's memory hooks (#1896): CONTINUE_PROMPT is submitted like a
 * prompt, so Claude Code fires `UserPromptSubmit` for it, and the recall would
 * run again for a message already in the transcript with what was recalled for
 * it. Without that hook; the retain at Stop stays. What harness/fallback.ts
 * `continuationRun` does to a run's hooks, done to the turn's, since a warm
 * process's hooks read the turn's from its slot (turnSlot.ts `slotHooks`).
 */
export function continuationMemory(memory: MemoryHooks): MemoryHooks {
  const { UserPromptSubmit: _submit, ...hooks } = memory.hooks
  return { ...memory, hooks }
}
