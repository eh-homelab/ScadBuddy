import type { Owner } from '../../sessions/protocol.js'
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
  message?: Message
  continuation?: Continuation
  traceparent?: string
}

export type SessionTurnResult = {
  status: 'done' | 'released' | 'failed' | 'interrupted' | 'budget_exhausted'
  hostQueue: string
  parked?: string[]
  error?: string
}

export type TurnOutcome = 'done' | 'interrupted' | 'budget_exhausted' | 'failed'

export type FinishTurnInput = { sessionId: string; turnId: string; outcome: TurnOutcome; message?: string }

/** The ApplicationFailure type a turn that spent the chat's budget raises. */
export const BUDGET_EXHAUSTED = 'BudgetExhausted'
export const MESSAGE_MAX = 500
export const INTERRUPTED = 'interrupted'
