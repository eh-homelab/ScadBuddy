import { z } from 'zod'

// The tab socket's wire protocol, version 1 (#254): `GET /api/v1/ai/bridge`
// (routes/bridge.ts), one JSON object per text frame. The tab's side is
// frontend/src/agent/link.ts, whose own schema is
// frontend/src/agent/linkProtocol.ts; test/bridge.e2e.test.ts runs that real
// module against this service, so the two cannot drift silently.
//
//   tab → agent   hello            first frame: the tab's id, route and live tools
//                 state            the route or the live tools changed
//                 result           the answer to a `call`, as the tab's
//                                  AgentBridge.call() gives it (never a throw)
//                 pairing.accept   the user typed a pairing code (spec §8.5)
//                 pairing.deny     the user turned a request down
//                 pairing.end      the user disconnected a paired agent
//   agent → tab   call             run one tool of the tab's catalogue
//                 pairings         the requests waiting and this tab's pairings
//                 pairing.result   how an accept went
//                 error            a frame was refused

export const LINK_PROTOCOL_VERSION = 1 as const

const v = z.literal(LINK_PROTOCOL_VERSION)
const route = z.string().max(2000)
const live = z.array(z.string().max(100)).max(100)
const id = z.string().min(1).max(100)

/** What the tab's `AgentBridge.call()` answers (frontend src/agent/types.ts `CallResult`). */
export const CallOutcomeSchema = z.union([
  z.object({ ok: z.literal(true), result: z.unknown() }),
  z.object({
    ok: z.literal(false),
    error: z.object({
      code: z.string().max(40),
      message: z.string().max(4000),
      issues: z.array(z.object({ path: z.string().max(200), message: z.string().max(500) })).max(50).optional(),
    }),
  }),
])
export type CallOutcome = z.infer<typeof CallOutcomeSchema>

export const TabFrameSchema = z.discriminatedUnion('type', [
  // 128 bits or more, base64url or hex (frontend link.ts `newTabId`).
  z.object({ v, type: z.literal('hello'), tabId: z.string().regex(/^[A-Za-z0-9_-]{22,64}$/), route, live }),
  z.object({ v, type: z.literal('state'), route, live }),
  z.object({ v, type: z.literal('result'), id, outcome: CallOutcomeSchema }),
  z.object({ v, type: z.literal('pairing.accept'), id, code: z.string().min(1).max(40) }),
  z.object({ v, type: z.literal('pairing.deny'), id }),
  z.object({ v, type: z.literal('pairing.end'), id }),
])
export type TabFrame = z.infer<typeof TabFrameSchema>

/** A pairing as the tab shows it; `expiresAt` is ISO 8601. */
export type PairingEntry = { id: string; label: string; expiresAt: string }

export type AgentFrame = { v: typeof LINK_PROTOCOL_VERSION } & (
  | { type: 'call'; id: string; tool: string; args: Record<string, unknown> }
  | { type: 'pairings'; pending: PairingEntry[]; paired: PairingEntry[] }
  | { type: 'pairing.result'; id: string; ok: boolean; message: string }
  | { type: 'error'; message: string }
)

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never

export function agentFrame(body: DistributiveOmit<AgentFrame, 'v'>): AgentFrame {
  return { v: LINK_PROTOCOL_VERSION, ...body } as AgentFrame
}

export type ParseResult<T> = { ok: true; value: T } | { ok: false; error: string }

/** A text frame from the tab. Never throws. */
export function parseTabFrame(raw: string): ParseResult<TabFrame> {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return { ok: false, error: 'frame is not JSON' }
  }
  if (typeof value === 'object' && value !== null && 'v' in value && value.v !== LINK_PROTOCOL_VERSION) {
    return { ok: false, error: `unsupported protocol version ${String(value.v)}` }
  }
  const parsed = TabFrameSchema.safeParse(value)
  return parsed.success ? { ok: true, value: parsed.data } : { ok: false, error: z.prettifyError(parsed.error) }
}
