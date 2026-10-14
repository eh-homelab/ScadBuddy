import { BlockList } from 'node:net'
import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import { OriginConfigError, type OriginPolicy, parseCidr, parseCidrList } from '../http/origins.js'
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
// The broadest range a peer entry may name: a /8 (v4) or /32 (v6). `0.0.0.0/0` would open
// the route to every network peer, leaving only the token.
const MIN_PREFIX = { 4: 8, 6: 32 } as const

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
  peers: z
    .array(
      z
        .string()
        .max(64)
        .transform((p) => p.trim())
        .refine(isPeer, 'each entry must be one IP address or CIDR range, no broader than a /8 (IPv4) or /32 (IPv6)'),
    )
    .max(MAX_PEERS),
})

// IPv4-mapped IPv6 (`::ffff:a.b.c.d`). A BlockList's IPv6 subnet that covers part of it also
// matches those IPv4 peers, so `::/32` or `::ffff:0.0.0.0/96` would admit every IPv4 peer.
const MAPPED = { address: '::ffff:0:0', prefix: 96 } as const

/** One address or range, as parseCidrList reads it, and no broader than MIN_PREFIX. */
function isPeer(entry: string): boolean {
  const cidr = entry.includes(',') ? undefined : parseCidr(entry)
  if (!cidr) return false
  const { address, prefix, family } = cidr
  if (family === 4) return prefix >= MIN_PREFIX[4]
  // An IPv6 range that overlaps the mapped block takes the IPv4 floor inside it (96 + 8).
  const overlapsMapped =
    prefix <= MAPPED.prefix ? inRange(address, prefix, MAPPED.address) : inRange(MAPPED.address, MAPPED.prefix, address)
  return prefix >= (overlapsMapped ? MAPPED.prefix + MIN_PREFIX[4] : MIN_PREFIX[6])
}

function inRange(network: string, prefix: number, address: string): boolean {
  const list = new BlockList()
  list.addSubnet(network, prefix, 'ipv6')
  return list.check(address, 'ipv6')
}

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
  return peers.length === 0 || !peers.every(isPeer) ? undefined : peerList(peers)
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
