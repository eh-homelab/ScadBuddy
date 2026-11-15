import type { Owner, QueuedEntry, SentImage, ServerEvent } from '../../sessions/protocol.js'
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
  /** The user's own words, without the page context `text` adds: what memory is recalled against (AgentSession only). */
  words?: string
  /** The sender's trace (a browser frame's, an MCP call's), which the turn's spans are children of (AgentSession only). */
  traceparent?: string
}

export type SendAnswer = { accepted: boolean; turn_id: string }

/**
 * A message sent while the session runs a turn (#6386): the `queue_message` Update's argument and
 * an entry of the workflow's queue. Its id is `turn_id`, the turn it becomes. `words` is what
 * `user.turn` shows (an edit replaces it), and `text` is `words` plus `context`, the page context
 * the panel sent, which an edit keeps. Images go by name only (#2479): `images` for the turn,
 * `sent` (full and preview) for `user.turn`. The row's claim columns as a send sets them
 * (durable.ts `DurableTurns.send`): `tiers` (`turn_tiers`) and `bare` (`turn_flow`).
 */
export type QueuedMessage = Message & {
  words: string
  context?: string
  sent?: SentImage[]
  tiers?: string[] | null
  bare?: boolean
  queued_at: string
}

/** A queued message as the session's readers see it (the `queue` Query, the `session.queue` event): no page context, no claim columns. */
export type QueuedView = QueuedEntry

/**
 * `queue_message`'s answer: the entry's id, its place (1 is next; 0 when it has left the queue,
 * for an Update sent again after its entry started or was removed), and the queue's version.
 */
export type QueueAnswer = { id: string; position: number; version: number }

/** The `edit_queued` Update's argument: the entry and its new words. */
export type EditQueuedArgs = { id: string; words: string }

/** The `queue` Query's answer, and what `session.queue_changed` writes. */
export type QueueState = { version: number; queue: QueuedView[] }

/** `session.queue_changed`'s argument: the queue as it is after a change. */
export type QueueChangedInput = { sessionId: string } & QueueState

/**
 * `session.begin_turn`'s argument: the queue's head, which a message the workflow holds no turn
 * for starts (#6386); and `session.finish_turn`'s `next`, which the turn's end starts in its
 * transaction.
 */
export type BeginTurnInput = { sessionId: string; next: QueuedMessage }

/**
 * Whether the head started: `started` (the row is its turn's), `busy` (the row runs another
 * turn: a send claimed it meanwhile, or a turn the sweep has not ended yet; the head waits), or
 * `dropped` (the session cannot run it: spent, done, archived, or its author no longer owns it;
 * an `error` event says why).
 */
export type BeginOutcome = 'started' | 'busy' | 'dropped'

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

/** The `tab_reconnected` Update's argument: every tab wait of the session, or only `request_id`'s (a turn's own early check). */
export type TabReconnectedArgs = { request_id?: string }

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
  /** The messages waiting for their turns (#6386), carried over a continue-as-new, and the queue's version. */
  queue?: QueuedMessage[] | null
  queue_version?: number | null
  /** The ids that left the queue lately (started, removed or dropped), so an Update sent again does not queue one twice. */
  queue_left?: string[] | null
  /** A queued message the last turn's end started (the row is its turn's), carried to the run that runs it. */
  message?: Message | null
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
  /** How long a parked call holds the turn's process before the turn releases it (`park_hold_seconds`, §5.4). */
  parkHoldSeconds?: number
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
export type FinishTurnInput = {
  sessionId: string
  turnId: string
  outcome: TurnOutcome
  message?: string
  code?: string
  tail?: ServerEvent[]
  /** The queue's head (#6386): started in the end's own transaction, so no send takes the idle row first. */
  next?: QueuedMessage
}

/** `session.finish_turn`'s answer when it was given `next`; a bare boolean (whether it wrote) otherwise, as before. */
export type FinishTurnResult = { written: boolean; next: BeginOutcome }

/** `acquire_worker`'s argument: the session, and the session host queues its turn was lost on (never placed there again). */
export type AcquireInput = { sessionId: string; exclude?: string[] }

/** The `error` code of a turn cut at `turn_max_seconds` (the activity's StartToClose): the session stays idle, as after classic's `error_max_turns`. */
export const TURN_MAX_SECONDS = 'turn_max_seconds'

/** The ApplicationFailure type a turn that spent the chat's budget raises. */
export const BUDGET_EXHAUSTED = 'BudgetExhausted'
export const MESSAGE_MAX = 500
/** How many messages may wait for their turns (#6386). */
export const QUEUE_MAX = 20
/** How many characters of JSON the queue may hold (#6386): it is carried in the workflow's state and over each continue-as-new, under Temporal's 2 MB payload limit. */
export const QUEUE_CHARS_MAX = 512 * 1024
/** The refusal of a send past QUEUE_MAX or QUEUE_CHARS_MAX. */
export const QUEUE_FULL = 'queue_full'
/** The refusal of a remove or an edit of an entry that is not queued (it started, or was removed). */
export const NOT_QUEUED = 'not_queued'
export const INTERRUPTED = 'interrupted'
