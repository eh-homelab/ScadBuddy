import { type BlockList, isIP } from 'node:net'
import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import { OriginConfigError, type OriginPolicy, parseCidrList } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './httpRequest.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/durable-subagents: where a durable segment's subagents reach
// ScadBuddy's tools (routes/subagentMcp.ts), for an agent-durable worker that is not
// in the agent's pod (its own Deployment, so it can scale).
//
//   GET /api/v1/ai/settings/durable-subagents   { url, peers }
//   PUT /api/v1/ai/settings/durable-subagents   body { url: string | null, peers: string[] }
//
// One `ai_settings` row, `durable_subagents`, `{ url, peers }`:
// `url`: the URL agent-durable gives its subagents, read at each segment. Null leaves
// its SCADBUDDY_DURABLE_SUBAGENT_MCP_URL (by default the pod's loopback) in force.
// `peers`: addresses or CIDR ranges, besides loopback,
// that the subagent route answers. Empty (the default) is loopback only. Widening it
// lets those peers present a token; the token is still required, minted under the KEK
// for one session, turn and owner, and every call is still checked against the session
// as it is when the call runs. Narrow the network to the agent-durable pods with a
// NetworkPolicy on the agent's port.
//
// Guarded and audited like the other AI settings routes (sessionMode.ts).

export const DURABLE_SUBAGENTS_PATH = '/api/v1/ai/settings/durable-subagents'
// agent-durable reads it too (scadbuddy_durable/session/runner.py SUBAGENTS_KEY).
export const SETTING_DURABLE_SUBAGENTS = 'durable_subagents'

export type DurableSubagentsView = { url: string | null; peers: string[] }

export type DurableSubagentsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const MAX_PEERS = 32
/**
 * The widest range a peer entry may name (#2881): `0.0.0.0/0` or `::/0` would open the
 * subagent route to every peer, leaving the per-turn token its only guard.
 */
const MIN_PREFIX = { 4: 8, 6: 32 } as const

/** What is wrong with one peer entry, or undefined when it is one address or range. */
function peerProblem(entry: string): string | undefined {
  if (entry === '' || entry.includes(',')) return 'each entry must be one IP address or CIDR range'
  if (peerList([entry]) === undefined) return 'each entry must be an IP address or CIDR range'
  const [address = '', prefixText] = entry.split('/')
  const family = isIP(address) as 4 | 6
  if (prefixText !== undefined && Number(prefixText) < MIN_PREFIX[family]) {
    return `a range may be no wider than /${MIN_PREFIX[family]} for IPv${family}`
  }
  return undefined
}

const PeerEntry = z
  .string()
  .max(64)
  .transform((s) => s.trim())
  .superRefine((entry, ctx) => {
    const problem = peerProblem(entry)
    if (problem) ctx.addIssue({ code: 'custom', message: problem })
  })

const PutBody = z.strictObject({
  url: z
    .string()
    .max(2048)
    .refine((s) => {
      try {
        return ['http:', 'https:'].includes(new URL(s).protocol)
      } catch {
        return false
      }
    }, 'must be an http(s) URL')
    .nullable(),
  peers: z.array(PeerEntry).max(MAX_PEERS),
})

/** The stored peers as a list, or undefined when any entry is not an address or range. */
function peerList(peers: readonly string[]): BlockList | undefined {
  try {
    return parseCidrList(peers.join(','), 'peers')
  } catch (err) {
    if (err instanceof OriginConfigError) return undefined
    throw err
  }
}

/** The stored row as the route writes it; anything else reads as unset. */
function stored(raw: unknown): DurableSubagentsView {
  if (typeof raw !== 'object' || raw === null) return { url: null, peers: [] }
  const { url, peers } = raw as Record<string, unknown>
  return {
    url: typeof url === 'string' ? url : null,
    peers: Array.isArray(peers) && peers.every((p) => typeof p === 'string') ? peers : [],
  }
}

/**
 * The peers besides loopback that the subagent route answers. A stored value this route
 * could not have written answers none (loopback only).
 */
export async function subagentPeers(settings: SettingsRepo | undefined): Promise<BlockList | undefined> {
  if (!settings) return undefined
  const { peers } = stored(await settings.get<unknown>(SETTING_DURABLE_SUBAGENTS))
  // Held to what the route accepts now (#2881): a range stored before the floor answers none.
  if (peers.length === 0 || peers.some((p) => peerProblem(p) !== undefined)) return undefined
  return peerList(peers)
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = "where durable subagents reach ScadBuddy's tools"

export function registerDurableSubagentsRoutes(app: Hono, deps: DurableSubagentsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  const view = async (settings: SettingsRepo) => stored(await settings.get<unknown>(SETTING_DURABLE_SUBAGENTS))

  app.get(DURABLE_SUBAGENTS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    return c.json(await view(settings))
  })

  app.put(DURABLE_SUBAGENTS_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    let body: z.infer<typeof PutBody>
    try {
      body = PutBody.parse(await c.req.json())
    } catch (err) {
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    const value: DurableSubagentsView = { url: body.url, peers: body.peers }
    await settings.set(SETTING_DURABLE_SUBAGENTS, value, {
      actor: UI_ACTOR,
      surface: 'http',
      clientIp: deps.remoteAddress(c),
    })
    return c.json(value)
  })
}

/** Settings → Assistant's durable subagents. `deps.settings` is declared in routes/httpRequest.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerDurableSubagentsRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
