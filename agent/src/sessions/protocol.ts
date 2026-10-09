// The server → panel events of the assistant panel's wire protocol, version 1,
// as the agent emits them. The contract is the panel's own zod schema in
// frontend/src/agent/chat/protocol.ts (#340); this is a type-level mirror of
// its `ServerEventSchema` for the session layer (#300), because agent/ and
// frontend/ are separate packages. test/sessions.protocol.test.ts parses every
// event the session layer produces with the frontend's schema itself, so the
// two cannot drift silently.

export const PROTOCOL_VERSION = 1 as const

/** Spec §6 origins. */
export const ORIGINS = ['chat', 'mcp', 'analyzer', 'hook'] as const
export type Origin = (typeof ORIGINS)[number]

/** Spec §6 statuses. */
export const SESSION_STATUSES = ['running', 'waiting_input', 'waiting_approval', 'idle', 'done', 'failed'] as const
export type SessionStatus = (typeof SESSION_STATUSES)[number]

export type Risk = 'read' | 'write' | 'outward'

/**
 * A principal (spec §8.1) as the protocol carries it; `label` is what the
 * "controlled by …" badge reads. Two owners are the same principal when kind
 * and id match.
 */
export type Owner = {
  kind: 'browser' | 'bearer' | 'oidc' | 'anonymous' | 'flow'
  id: string
  label: string
}

/** A question as the panel shows it (#940, harness/questions.ts `UserQuestion`). */
export type QuestionView = {
  question: string
  header: string
  multiSelect: boolean
  /** `preview`: Markdown the option shows, e.g. a draft to approve. */
  options: { label: string; description: string; preview?: string }[]
}

/** #815: what makes a `question.asked` an attention request. */
export type AttentionView =
  | {
      reason: 'tab_disconnected' | 'question' | 'blocked' | 'done'
      /** What the timer does at `expiresAt` (ISO 8601): it never answers. */
      onTimeout: 'proceed' | 'wait' | 'stop'
      expiresAt: string
    }
  /**
   * A `done` summary (#815 §4): no timer, nothing waits on it, and it stays
   * until the user dismisses it. `summary` is ScadBuddy's record of what the
   * turn touched (Markdown, questions/doneSummary.ts). A replica on an older
   * image sends a done request in the shape above.
   */
  | { reason: 'done'; summary: string }

/** An image a tool returned, as the event names it (#782): `<sha256>.<ext>`, one of the session's blobs. */
export type ImageRef = { name: string; mediaType: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp' }

export type SessionSummary = {
  sessionId: string
  title: string
  origin: Origin
  owner: Owner
  status: SessionStatus
}

type V = { v: typeof PROTOCOL_VERSION }

/**
 * One tool call parked on a person (durable-agents spec §6.6), in the one shape both
 * modes and both reads use. `id` is the `request_id` respond takes, opaque to clients:
 * `approval:<uuid>`, `question:<uuid>` (a classic row) or
 * `durable:<session id>:<workflow run id>:<tool_use_id>`.
 */
export type InputEntry = {
  id: string
  kind: 'approval' | 'answer'
  session_id: string | null
  tool: string
  /** An approval's scrubbed summary; empty for an answer. Never the call's raw input. */
  summary: string
  input_hash: string | null
  /** An answer's question or attention message, scrubbed; empty for an approval. */
  prompt: string
  /** Who asked for the approval; null for an answer (the session's agent). */
  requested_by: Owner | null
  /** Who may answer: the browser user, and for an approval also a grant holder. */
  responders: ('browser' | 'grant')[]
  created_at: string
  /** When its timer fires; null for an entry with none (a `done` summary). */
  expires_at: string | null
  /** Set on an attention request (#815) only. */
  attention?: { reason: string; on_timeout: string | null; summary?: string }
}

/** How a parked call ended (`ai_input_responses.outcome`). */
export type InputOutcome = 'approved' | 'denied' | 'expired' | 'answered' | 'cancelled' | 'timed_out'

export type ServerEvent = V &
  (
    | { type: 'sessions.snapshot'; sessions: SessionSummary[] }
    /** `budgetUsd`: what the session may spend in all (#790); absent on sessions started before it. */
    | { type: 'session.started'; sessionId: string; origin: Origin; owner: Owner; title?: string; budgetUsd?: number }
    | { type: 'session.owner'; sessionId: string; owner: Owner }
    /** `images`: previews of the images the user sent with the turn (#1866, images.ts); the model got the full ones. */
    | {
        type: 'user.turn'
        sessionId: string
        turnId: string
        text: string
        author: Owner
        images?: { mediaType: 'image/png' | 'image/jpeg' | 'image/webp'; data: string }[]
      }
    | { type: 'assistant.text.delta'; sessionId: string; messageId: string; delta: string }
    | { type: 'assistant.text.done'; sessionId: string; messageId: string }
    /**
     * `parent`: a subagent's call (#1108), the id of the session's `Agent` call that spawned it.
     * `title`: what the call does, in words (#782): the registry tool's own (tools/registry.ts
     * `ToolSpec.title`); absent for a tool outside the registry, which the panel names itself.
     */
    | {
        type: 'tool.call'
        sessionId: string
        id: string
        name: string
        input: Record<string, unknown>
        risk: Risk
        parent?: string
        title?: string
      }
    /**
     * `images`: the images the result carried (#782), each served by
     * `GET /api/v1/ai/sessions/:id/blobs/:name` (sessions/blobs.ts); the bytes are never in the event.
     */
    | { type: 'tool.result'; sessionId: string; id: string; ok: boolean; summary: string; images?: ImageRef[] }
    /** `tool` is the tool.call id the approval gates; only outward calls wait (spec §8.2). */
    | { type: 'approval.required'; sessionId: string; id: string; tool: string; summary: string; risk: 'outward' }
    /**
     * `decision` says how it ended (#979): expired and cancelled ones are not
     * approved, have no `by`, and carry why (`reason`); `approved` stays for
     * readers of the log that predate `decision`.
     */
    | {
        type: 'approval.resolved'
        sessionId: string
        id: string
        approved: boolean
        decision: 'approved' | 'denied' | 'expired' | 'cancelled'
        by?: Owner
        reason?: string
      }
    /**
     * The agent asks the user (#940): `tool` is the AskUserQuestion (or a subagent's ask_user) tool_use id,
     * a `tool.call` in the feed (a subagent's since #1108).
     * The turn waits (`waiting_input`) until `question.resolved`.
     */
    | {
        type: 'question.asked'
        sessionId: string
        id: string
        tool: string
        questions: QuestionView[]
        /**
         * #815: an attention request (harness/attention.ts), not a question: one
         * card, and a timer that resolves it at `expiresAt` without an answer.
         */
        attention?: AttentionView
      }
    /**
     * Answered (`answers`: one per question, in order, and who answered), or
     * not: cancelled with its turn, with `reason`. Never answered by itself.
     */
    | {
        type: 'question.resolved'
        sessionId: string
        id: string
        answered: boolean
        answers?: string[]
        by?: Owner
        reason?: string
        /** #815 §2: a `tab_disconnected` attention request ended because the session's tab is connected again. */
        reconnected?: true
      }
    /**
     * A call parked on a person, of any kind and in either mode (spec §6.6,
     * "Notifications"): what notifications are driven by. Logged and announced on the
     * bus beside the per-kind events the panel's cards use; never sent on the chat socket.
     */
    | { type: 'input.requested'; sessionId: string; entry: InputEntry }
    /** The entry `id` ended: answered, decided, timed out or cancelled. */
    | { type: 'input.resolved'; sessionId: string; id: string; kind: 'approval' | 'answer'; outcome: InputOutcome; reason?: string }
    | { type: 'session.status'; sessionId: string; status: SessionStatus }
    | { type: 'session.result'; sessionId: string; costUsd?: number; turns: number; budgetUsd?: number }
    /** The budget changed (a raise, #790), or a send was refused because it is spent. */
    | { type: 'session.budget'; sessionId: string; costUsd: number; budgetUsd: number }
    /** `questionId`: the error refused the panel's answer to that question (#940), so its card is answerable again. */
    | { type: 'error'; sessionId?: string; code?: string; message: string; questionId?: string }
    /**
     * An automatic Hindsight recall or retain (#818, memory/hindsight.ts). A
     * retain finishes after its turn, so this can follow the turn's last
     * status. `count` is a recall's; `detail` is why one failed, redacted.
     * `input` is what was sent (a recall's query, the start of a retain's
     * content) and `memories` what a recall injected, both redacted and capped
     * (MEMORY_TEXT_MAX); the panel shows them only in Advanced mode.
     */
    | {
        type: 'memory'
        sessionId: string
        turnId: string
        action: 'recall' | 'retain'
        bank: string
        outcome: 'ok' | 'timeout' | 'error'
        count?: number
        detail?: string
        input?: string
        memories?: string[]
      }
  )

export type ServerEventType = ServerEvent['type']

/** Distributes Omit over the union, so `event({ type: ..., ... })` checks each variant. */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never

/** Stamps the protocol version. */
export function event(body: DistributiveOmit<ServerEvent, 'v'>): ServerEvent {
  return { v: PROTOCOL_VERSION, ...body } as ServerEvent
}

export function sameOwner(a: Pick<Owner, 'kind' | 'id'>, b: Pick<Owner, 'kind' | 'id'>): boolean {
  return a.kind === b.kind && a.id === b.id
}

/**
 * Spec §6 visibility: the browser user sees every session (with a "controlled
 * by …" badge); any other principal sees the sessions it owns or started, and
 * one offered to it (a pending handoff, manager.ts `handoff`), so it can read
 * what it is asked to take over before it accepts. `offer` is only ever a
 * live one: the manager leaves an expired offer out.
 */
export function canSee(
  principal: Owner,
  session: {
    owner: Pick<Owner, 'kind' | 'id'>
    creator: Pick<Owner, 'kind' | 'id'>
    offer?: { to: Pick<Owner, 'kind' | 'id'> } | null
  },
): boolean {
  return (
    principal.kind === 'browser' ||
    sameOwner(principal, session.owner) ||
    sameOwner(principal, session.creator) ||
    (session.offer != null && sameOwner(principal, session.offer.to))
  )
}

/** A principal as another principal is shown it: without its id when that is not the viewer's own. */
export type SeenOwner = { kind: Owner['kind']; id?: string; label: string }

/**
 * What a label may say to a principal that is not the one it names. An MCP
 * token's and an OIDC principal's labels carry their id (approvals/mcp.ts
 * `ownerOf`: `MCP token:<id>`, `MCP OIDC <sub>`), and an anonymous client's its
 * address, so they are replaced by their kind; the browser user's ("You") and a
 * flow's name no MCP principal.
 */
export function publicLabel(owner: Owner): string {
  switch (owner.kind) {
    case 'bearer':
      return 'another MCP token'
    case 'oidc':
      return 'another MCP OIDC principal'
    case 'anonymous':
      return 'an anonymous MCP client'
    default:
      return owner.label
  }
}

/**
 * PR #715 review: a principal id is what `sessions_handoff` addresses, so an
 * MCP caller is shown only its own. Anyone else in a session (its owner, a
 * pending offer's target, a turn's author, a decider) is shown by kind and a
 * label that does not name it (`publicLabel`). The browser user sees every id
 * (spec §6: it sees every session; the panel's badges and Take over read them),
 * and the browser user's own id, `browser`, names nobody's token.
 */
export function ownerSeenBy(viewer: Pick<Owner, 'kind' | 'id'>, owner: Owner): SeenOwner {
  if (viewer.kind === 'browser' || owner.kind === 'browser' || sameOwner(viewer, owner)) return owner
  return { kind: owner.kind, label: publicLabel(owner) }
}

/**
 * The panel's `approval.decision` client message (frontend protocol.ts
 * `ClientMessageSchema`), the one client message the agent consumes today
 * (#258; the socket that carries it is #266's).
 */
export type ApprovalDecisionMessage = V & { type: 'approval.decision'; sessionId: string; id: string; approve: boolean }

/** The panel's `question.answer` (#940): one answer per question, in the order asked. */
export type QuestionAnswerMessage = V & { type: 'question.answer'; sessionId: string; id: string; answers: string[] }
