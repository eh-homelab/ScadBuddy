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
    | { type: 'session.started'; sessionId: string; origin: Origin; owner: Owner; title?: string }
    | { type: 'session.owner'; sessionId: string; owner: Owner }
    | { type: 'user.turn'; sessionId: string; turnId: string; text: string; author: Owner }
    | { type: 'assistant.text.delta'; sessionId: string; messageId: string; delta: string }
    | { type: 'assistant.text.done'; sessionId: string; messageId: string }
    | { type: 'tool.call'; sessionId: string; id: string; name: string; input: Record<string, unknown>; risk: Risk }
    | { type: 'tool.result'; sessionId: string; id: string; ok: boolean; summary: string }
    | { type: 'session.status'; sessionId: string; status: SessionStatus }
    | { type: 'session.result'; sessionId: string; costUsd?: number; turns: number }
    | { type: 'error'; sessionId?: string; code?: string; message: string }
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
