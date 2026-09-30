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

export type SessionSummary = {
  sessionId: string
  title: string
  origin: Origin
  owner: Owner
  status: SessionStatus
}

type V = { v: typeof PROTOCOL_VERSION }

export type ServerEvent = V &
  (
    | { type: 'sessions.snapshot'; sessions: SessionSummary[] }
    /** `budgetUsd`: what the session may spend in all (#790); absent on sessions started before it. */
    | { type: 'session.started'; sessionId: string; origin: Origin; owner: Owner; title?: string; budgetUsd?: number }
    | { type: 'session.owner'; sessionId: string; owner: Owner }
    | { type: 'user.turn'; sessionId: string; turnId: string; text: string; author: Owner }
    | { type: 'assistant.text.delta'; sessionId: string; messageId: string; delta: string }
    | { type: 'assistant.text.done'; sessionId: string; messageId: string }
    | { type: 'tool.call'; sessionId: string; id: string; name: string; input: Record<string, unknown>; risk: Risk }
    | { type: 'tool.result'; sessionId: string; id: string; ok: boolean; summary: string }
    /** `tool` is the tool.call id the approval gates; only outward calls wait (spec §8.2). */
    | { type: 'approval.required'; sessionId: string; id: string; tool: string; summary: string; risk: 'outward' }
    /** Expired and cancelled approvals resolve as not approved, without `by`. */
    | { type: 'approval.resolved'; sessionId: string; id: string; approved: boolean; by?: Owner }
    | { type: 'session.status'; sessionId: string; status: SessionStatus }
    | { type: 'session.result'; sessionId: string; costUsd?: number; turns: number; budgetUsd?: number }
    /** The budget changed (a raise, #790), or a send was refused because it is spent. */
    | { type: 'session.budget'; sessionId: string; costUsd: number; budgetUsd: number }
    | { type: 'error'; sessionId?: string; code?: string; message: string }
    /**
     * An automatic Hindsight recall or retain (#818, memory/hindsight.ts). A
     * retain finishes after its turn, so this can follow the turn's last
     * status. `count` is a recall's; `detail` is why one failed, redacted.
     * Never the query or a memory.
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
