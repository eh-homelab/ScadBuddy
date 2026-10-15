// The turn a session's Claude Code is serving now (unified runtime spec
// 2026-10-13 §5.3, "Turn-scoped callbacks go through a slot").
//
// Every option `query()` is spawned with is fixed for the life of its process,
// so no callback in it may hold one turn's state. TurnRunner (turnRunner.ts)
// assembles callbacks that read `slot.current` instead: the approval and
// question gates, the memory and trace hooks, the in-process servers' turn
// context and tab wait, and the http tool's turn id and abort signal. A warm
// process (warm.ts) serves many turns: each sets the slot before it pushes its
// prompt and clears it once it has finished, its events written (manager.ts
// `runTurn`), so a hook Claude Code fires after that, between turns, finds no
// turn and fails closed. A retain the Stop hook started before the result
// goes on without the slot (memory/hindsight.ts: fire-and-forget, with the
// turn's own closures).

import type { HookCallbackMatcher, HookEvent } from '@anthropic-ai/claude-agent-sdk'
import type { TurnAuditor } from '../audit/turn.js'
import type { WebContext } from '../builtins/web.js'
import type { HttpRequestContext } from '../harness/httpRequest.js'
import type { ApprovalGate } from '../harness/permissions.js'
import type { QuestionGate } from '../harness/questions.js'
import type { MemoryHooks } from '../memory/hindsight.js'
import type { TurnTrace } from '../telemetry/turn.js'
import type { WaitForTab } from '../tools/registry.js'
import type { TurnPrincipal } from './manager.js'

/** What every slot-reading callback answers, or is refused with, when no turn is running. */
export const NO_TURN = 'no turn is running'

/** The http tool's per-turn state (harness/httpRequest.ts `HttpRequestContext`). */
export type HttpState = {
  turnId: string
  /** Aborts the turn's requests in flight. */
  signal: AbortSignal
}

export type TurnContext = {
  turnId: string
  principal: TurnPrincipal
  /** The turn's approval gate, already wrapped by its auditor when there is one. */
  approvalGate: ApprovalGate
  /** Only in a session the browser user owns (#940). */
  questionGate?: QuestionGate
  auditor?: TurnAuditor
  trace: TurnTrace
  http?: HttpState
  /** What the turn's WebFetch model calls cost (builtins/nested.ts), which no result prices (#2684). */
  nested?: { usd: number }
  /** #815 §2: how this turn's browser_* calls wait for a missing tab; only with a question gate. */
  waitForTab?: WaitForTab
  /** The turn's automatic memory hooks (memory/hindsight.ts), built for its own prompt. */
  memory?: MemoryHooks
  signalInterrupt(): void
}

export type TurnSlot = {
  current?: TurnContext
  set(ctx: TurnContext): void
  clear(): void
}

export function createTurnSlot(): TurnSlot {
  const slot: TurnSlot = {
    set(ctx) {
      slot.current = ctx
    },
    clear() {
      delete slot.current
    },
  }
  return slot
}

/**
 * The http tool's context (harness/httpRequest.ts): `fixed`, with the turn's
 * id and abort signal read from the slot as each request runs. With no turn
 * the signal is already aborted, so a request is never sent.
 */
export function slotHttpContext(fixed: Omit<HttpRequestContext, 'turnId' | 'signal'>, slot: TurnSlot): HttpRequestContext {
  return {
    ...fixed,
    get turnId() {
      return slot.current?.http?.turnId
    },
    get signal() {
      return slot.current?.http?.signal ?? AbortSignal.abort(new Error(NO_TURN))
    },
  }
}

/**
 * WebFetch's context (builtins/web.ts), as `slotHttpContext`: the turn's id and
 * abort signal read from the slot as each fetch runs.
 */
export function slotWebContext(fixed: Omit<WebContext, 'turnId' | 'signal'>, slot: TurnSlot): WebContext {
  return {
    ...fixed,
    get turnId() {
      return slot.current?.http?.turnId
    },
    get signal() {
      return slot.current?.http?.signal ?? AbortSignal.abort(new Error(NO_TURN))
    },
  }
}

type Hooks = Partial<Record<HookEvent, HookCallbackMatcher[]>>

/**
 * Hooks shaped like `shape` (the same events, matchers and timeouts), each
 * callback running the one in the same place of the current turn's hooks
 * (`pick`). With no turn, or none there, a callback does nothing.
 */
export function slotHooks(shape: Hooks, slot: TurnSlot, pick: (turn: TurnContext) => Hooks | undefined): Hooks {
  const hooks: Hooks = {}
  for (const [event, matchers] of Object.entries(shape) as [HookEvent, HookCallbackMatcher[]][]) {
    hooks[event] = matchers.map((m, i) => ({
      ...m,
      hooks: m.hooks.map((_, j) => (input, toolUseId, options) => {
        const turn = slot.current
        const hook = turn ? pick(turn)?.[event]?.[i]?.hooks[j] : undefined
        return hook ? hook(input, toolUseId, options) : Promise.resolve({})
      }),
    }))
  }
  return hooks
}
