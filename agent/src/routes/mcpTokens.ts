import type { Hono } from 'hono'
import { z } from 'zod'
import type { McpAuthMode, McpAuthSettings } from '../auth/authenticate.js'
import { TIERS } from '../auth/principal.js'
import type { TokenRecord, TokenStore } from '../auth/tokens.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'

// /api/v1/ai/mcp-tokens (#251, spec §8.1 "minted in Settings, stored hashed",
// §8.3): Settings mints, lists and revokes the bearer tokens `/mcp` accepts.
//
// - GET lists metadata only: name, tier, the four timestamps and a derived
//   status. The store keeps no hint of the token (only its SHA-256), so there is
//   nothing else to show, and no route ever returns the hash.
// - POST returns the plaintext token exactly once, in its 201 body, marked
//   `Cache-Control: no-store`. It is never logged and cannot be read again.
// - DELETE /:id revokes. A revoked token stays listed, so Settings shows when it
//   was revoked; it never verifies again.
//
// Minting a token is a credential write, so outward tier (spec §8.1): writes pass
// guard.ts `uiRequestProblem`, as the credential routes do, and reads pass
// `uiReadProblem`. Every auth mode allows managing tokens (spec §8.3): in
// `disabled` mode `/mcp` does not check them, but they are kept for when the
// mode goes back to `bearer`, and in `oidc` mode bearer tokens keep working
// alongside the IdP. GET reports the mode so Settings can say which applies.
//
// Error bodies are `{ detail }`, like routes/credentials.ts.

export type McpTokenRouteDeps = {
  /** Undefined when there is no database (spec §9, "No database"). */
  tokens: TokenStore | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  /** The current MCP auth settings (the same function /mcp reads per request). */
  authSettings: () => McpAuthSettings | Promise<McpAuthSettings>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  /** Clock, for tests: decides `status: 'expired'`. */
  now?: () => Date
}

export type McpTokenStatus = 'active' | 'expired' | 'revoked'

export type McpTokenView = {
  id: string
  name: string
  tier: TokenRecord['tier']
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
  status: McpTokenStatus
}

export type McpTokenList = {
  /** Null when the auth settings could not be read. */
  auth_mode: McpAuthMode | null
  /** Newest first by `created_at`; a same-microsecond tie falls back to the random `id`. */
  tokens: McpTokenView[]
}

export type MintedMcpToken = {
  /** The bearer token. Shown once; the service keeps only its SHA-256. */
  token: string
  record: McpTokenView
}

export const MAX_TOKEN_NAME = 100
/** Ten years: a token that should never expire leaves `expires_in` out. */
export const MAX_EXPIRES_IN_SECONDS = 10 * 366 * 24 * 60 * 60

const PostBody = z.strictObject({
  name: z
    .string()
    .trim()
    .min(1, 'give the token a name')
    .max(MAX_TOKEN_NAME)
    // eslint-disable-next-line no-control-regex
    .refine((name) => !/[\u0000-\u001f\u007f]/.test(name), 'must not contain control characters'),
  tier: z.enum(TIERS),
  /** Seconds from now; left out, the token does not expire. */
  expires_in: z.number().int().min(60).max(MAX_EXPIRES_IN_SECONDS).optional(),
})

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

export function tokenView(record: TokenRecord, now: Date): McpTokenView {
  const status: McpTokenStatus = record.revokedAt
    ? 'revoked'
    : record.expiresAt && record.expiresAt.getTime() <= now.getTime()
      ? 'expired'
      : 'active'
  return {
    id: record.id,
    name: record.name,
    tier: record.tier,
    created_at: record.createdAt.toISOString(),
    expires_at: record.expiresAt?.toISOString() ?? null,
    last_used_at: record.lastUsedAt?.toISOString() ?? null,
    revoked_at: record.revokedAt?.toISOString() ?? null,
    status,
  }
}

export function registerMcpTokenRoutes(app: Hono, deps: McpTokenRouteDeps): void {
  const base = '/api/v1/ai/mcp-tokens'
  const now = deps.now ?? (() => new Date())

  async function store(): Promise<TokenStore | string> {
    if (!deps.tokens) return NO_DATABASE
    return (await deps.ready()) ? deps.tokens : NOT_READY
  }

  async function authMode(): Promise<McpAuthMode | null> {
    try {
      return (await deps.authSettings()).mode
    } catch {
      return null
    }
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'MCP token reads')
    if (problem) return c.json({ detail: problem }, 403)
    const tokens = await store()
    if (typeof tokens === 'string') return c.json({ detail: tokens }, 503)
    const at = now()
    const list = (await tokens.list()).map((record) => tokenView(record, at)).reverse()
    const body: McpTokenList = { auth_mode: await authMode(), tokens: list }
    c.header('Cache-Control', 'no-store')
    return c.json(body)
  })

  // Every write is outward tier; see guard.ts for what is and is not checked.
  app.on(['POST', 'DELETE', 'PUT', 'PATCH'], [base, `${base}/*`], async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'MCP token changes')
    if (problem) return c.json({ detail: problem }, 403)
    await next()
  })

  app.post(base, async (c) => {
    // As guard.ts does for PUT: a cross-origin HTML form cannot send JSON.
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (type !== 'application/json') return c.json({ detail: 'request body must be application/json' }, 415)
    const tokens = await store()
    if (typeof tokens === 'string') return c.json({ detail: tokens }, 503)
    let body: z.infer<typeof PostBody>
    try {
      body = PostBody.parse(await c.req.json())
    } catch (err) {
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    const at = now()
    const { token, record } = await tokens.mint({
      name: body.name,
      tier: body.tier,
      ...(body.expires_in === undefined ? {} : { expiresAt: new Date(at.getTime() + body.expires_in * 1000) }),
    })
    const minted: MintedMcpToken = { token, record: tokenView(record, at) }
    c.header('Cache-Control', 'no-store')
    return c.json(minted, 201)
  })

  app.delete(`${base}/:id`, async (c) => {
    const tokens = await store()
    if (typeof tokens === 'string') return c.json({ detail: tokens }, 503)
    if (!(await tokens.revoke(c.req.param('id')))) {
      return c.json({ detail: 'no live token has that id (it is unknown or already revoked)' }, 404)
    }
    return c.body(null, 204)
  })
}
