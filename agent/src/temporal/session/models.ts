import type { Owner, ServerEvent } from '../../sessions/protocol.js'
import type { FlowTurn } from '../../tools/registry.js'
import type { Resolution } from './gate.js'

// What crosses AgentSession's history (spec 2026-10-13 §5.1). `SessionStart` and
// `TurnStart` are agent-durable's session/models.py shapes (snake_case, as DurableTurns
// sends them), without its `agent` state: a unified turn keeps its transcript in
// ai_session_entries. The turn activity's input and result are camelCase, as the
// agent's other activities'. Free of Node imports: the workflow bundle loads it.

export type ImageRef = { name: string; mediaType: string }

/** The `send_message` Update's argument (durable.ts `DurableMessage`). */
export type Message = {
  turn_id: string
  text: string
  author: Owner
  images: ImageRef[]
  system_append: string | null
  flow?: FlowTurn
}

export type SendAnswer = { accepted: boolean; turn_id: string }

/** The turn that runs: carried across a continue-as-new in the middle of it. */
export type TurnStart = {
  turn_id: string
  author: Owner
  images?: ImageRef[]
  system_append?: string | null
  /** Why each call the gate refused was not run, by tool_use id (#2392). */
  refusals?: Record<string, string>
  started_at?: string | null
  flow?: FlowTurn | null
}

/** What the next `session_turn` must do first (spec §5.4, §5.5). */
export type Continuation =
  | { kind: 'restarted' }
  | { kind: 'parked'; calls: { requestId: string; toolUseId: string; tool: string; input: unknown; resolution: Resolution }[] }

/** The `open_input` Update's argument (spec §5.4): a call of the running turn that parks. */
export type OpenInputArgs = { turn_id: string; call: { id: string; name: string; input: unknown } }

/**
 * Its answer: the entry's request id, and, for a call settled at once with no entry to wait
 * on (a read turn tool, a run grant, the flow's refusal, a malformed question), how it ended.
 */
export type OpenInputAnswer = { request_id: string; resolution?: Resolution }

/** The `withdraw_input` Update's argument: the call of the running turn whose entry nothing waits on any more. */
export type WithdrawInputArgs = { turn_id: string; tool_use_id: string; reason: string }

export type SessionStart = {
  session_id: string
  creator: Owner
  owner: Owner
  max_turns: number
  system_append: string | null
  turn?: TurnStart | null
  /** A Stop recorded in the activation that continued as new, carried to the turn. */
  interrupted?: string | null
  /** The replica the session's last turn ran on, carried over a continue-as-new. */
  host_queue?: string | null
  continuation?: Continuation | null
}

export type SessionTurnInput = {
  sessionId: string
  turnId: string
  /**
   * This placement of the turn (the workflow's run id and cycle, §5.2): written on the row as it
   * claims the turn, so an earlier placement that still runs sees it has been replaced and stops.
   */
  attempt?: string
  message?: Message
  continuation?: Continuation
  traceparent?: string
}

export type SessionTurnResult = {
  status: 'done' | 'released' | 'failed' | 'interrupted' | 'budget_exhausted'
  hostQueue: string
  parked?: string[]
  error?: string
  /**
   * The turn's closing events as a classic turn builds them (turnCore.ts `tallyTurn`), scrubbed:
   * what `session.finish_turn` writes, worded with the row's spend and budget (`wordTail`).
   */
  tail?: ServerEvent[]
  /**
   * The `error` event's code a classic turn would write for this end (turnCore.ts `tallyTurn`):
   * `api_error`, a result subtype such as `error_max_turns`, or `turn_failed`. With `done` the
   * session stays idle and the error is still said (classic's `error_max_turns`).
   */
  code?: string
}

export type TurnOutcome = 'done' | 'interrupted' | 'budget_exhausted' | 'failed'

/**
 * `tail`: the turn's own closing events (`SessionTurnResult.tail`), when its activity ended it;
 * else `code` and `message`, the `error` event of an end the workflow decided (the time cap).
 */
export type FinishTurnInput = { sessionId: string; turnId: string; outcome: TurnOutcome; message?: string; code?: string; tail?: ServerEvent[] }

/** `acquire_worker`'s argument: the session, and the session host queues its turn was lost on (never placed there again). */
export type AcquireInput = { sessionId: string; exclude?: string[] }

/** The `error` code of a turn cut at `turn_max_seconds` (the activity's StartToClose): the session stays idle, as after classic's `error_max_turns`. */
export const TURN_MAX_SECONDS = 'turn_max_seconds'

/** The ApplicationFailure type a turn that spent the chat's budget raises. */
export const BUDGET_EXHAUSTED = 'BudgetExhausted'
export const MESSAGE_MAX = 500
export const INTERRUPTED = 'interrupted'
