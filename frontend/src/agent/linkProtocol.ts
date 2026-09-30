import { z } from 'zod'

/**
 * The browser bridge's tab socket, version 1 (#254): what this tab and the agent service
 * say to each other over `GET /api/v1/ai/bridge`, one JSON object per text frame. The
 * agent's side is `agent/src/bridge/protocol.ts`; its end-to-end test runs `link.ts`
 * against the real service, so the two cannot drift silently.
 *
 * Tab → agent: `hello` (first; the tab id, route and live tools), `state` (either changed),
 * `result` (the answer to a `call`), and the user's pairing answers `pairing.accept`,
 * `pairing.deny` and `pairing.end`. Agent → tab: `call`, `pairings`, `pairing.result` and
 * `error`. Anything that fails these schemas is dropped.
 */

export const LINK_PROTOCOL_VERSION = 1 as const

const v = z.literal(LINK_PROTOCOL_VERSION)

export const PairingEntrySchema = z.object({
  id: z.string().min(1),
  /** Who is asking, as the agent service names it (an MCP token's name, say). */
  label: z.string().min(1),
  expiresAt: z.string(),
})
export type PairingEntry = z.infer<typeof PairingEntrySchema>

export const AgentFrameSchema = z.discriminatedUnion('type', [
  z.object({
    v,
    type: z.literal('call'),
    id: z.string().min(1).max(100),
    tool: z.string().min(1).max(100),
    args: z.record(z.string(), z.unknown()),
  }),
  z.object({
    v,
    type: z.literal('pairings'),
    pending: z.array(PairingEntrySchema),
    paired: z.array(PairingEntrySchema),
  }),
  z.object({ v, type: z.literal('pairing.result'), id: z.string(), ok: z.boolean(), message: z.string() }),
  z.object({ v, type: z.literal('error'), message: z.string() }),
])
export type AgentFrame = z.infer<typeof AgentFrameSchema>

export type TabFrame = { v: typeof LINK_PROTOCOL_VERSION } & (
  | { type: 'hello'; tabId: string; route: string; live: string[] }
  | { type: 'state'; route: string; live: string[] }
  | { type: 'result'; id: string; outcome: unknown }
  | { type: 'pairing.accept'; id: string; code: string }
  | { type: 'pairing.deny'; id: string }
  | { type: 'pairing.end'; id: string }
)

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never

export function tabFrame(body: DistributiveOmit<TabFrame, 'v'>): TabFrame {
  return { v: LINK_PROTOCOL_VERSION, ...body } as TabFrame
}

/** A text frame from the agent. Never throws. */
export function parseAgentFrame(raw: unknown): { ok: true; value: AgentFrame } | { ok: false; error: string } {
  let value: unknown
  try {
    value = typeof raw === 'string' ? JSON.parse(raw) : raw
  } catch {
    return { ok: false, error: 'frame is not JSON' }
  }
  const parsed = AgentFrameSchema.safeParse(value)
  return parsed.success ? { ok: true, value: parsed.data } : { ok: false, error: z.prettifyError(parsed.error) }
}
