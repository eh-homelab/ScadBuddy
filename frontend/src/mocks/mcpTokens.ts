import { HttpResponse, http } from 'msw'
import type {
  McpAuthMode,
  McpAuthSetting,
  McpAuthUpdate,
  McpToken,
  McpTokenCreate,
  McpTokenTier,
  MintedMcpToken,
} from '../api/mcpTokens'

/**
 * The agent service's `/api/v1/ai/mcp-tokens` (#251, `agent/src/routes/mcpTokens.ts`)
 * for vitest and the mocked e2e run: the same shapes, validation and status codes.
 * Like the service, it keeps no plaintext: a minted token is only in the POST's answer.
 */

const base = '/api/v1/ai/mcp-tokens'
const TIERS: readonly McpTokenTier[] = ['read', 'write', 'outward']
const MAX_NAME = 100
const MAX_EXPIRES_IN = 10 * 366 * 24 * 60 * 60

function seed(): McpToken[] {
  return [
    {
      id: '6f1c2b1e-0d5c-4c7e-9a51-3b2f0b6f1a01',
      name: 'Claude Desktop',
      tier: 'outward',
      created_at: '2026-09-20T09:12:00Z',
      expires_at: null,
      last_used_at: '2026-09-27T18:40:00Z',
      revoked_at: null,
      status: 'active',
    },
    {
      id: '0b8e4d2a-7f3b-4a61-8c0d-5e9f2a7b3c02',
      name: 'Old laptop',
      tier: 'read',
      created_at: '2026-08-01T10:00:00Z',
      expires_at: '2026-09-01T10:00:00Z',
      last_used_at: null,
      revoked_at: '2026-08-15T12:30:00Z',
      status: 'revoked',
    },
  ]
}

const state = {
  /** Newest first, as the service lists them. */
  tokens: seed(),
  authMode: 'bearer' as McpAuthMode | null,
  /** `/api/v1/ai/mcp/auth`'s cap; its mode is `authMode`. */
  anonymousCap: 'outward' as McpTokenTier,
}

export function resetMcpTokens(): void {
  state.tokens = seed()
  state.authMode = 'bearer'
  state.anonymousCap = 'outward'
}

/** For tests: the mode GET reports (spec §8.3). */
export function setMcpAuthMode(mode: McpAuthMode | null): void {
  state.authMode = mode
}

function detail(status: number, message: string) {
  return HttpResponse.json({ detail: message }, { status })
}

function randomToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  const b64 = btoa(String.fromCharCode(...bytes))
  return `sbmcp_${b64.replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')}`
}

function problems(body: Partial<McpTokenCreate> & Record<string, unknown>): string | undefined {
  const extra = Object.keys(body).filter((key) => !['name', 'tier', 'expires_in'].includes(key))
  if (extra.length > 0) return `body: unrecognized key(s) ${extra.join(', ')}`
  const name = typeof body.name === 'string' ? body.name.trim() : ''
  if (!name) return 'name: give the token a name'
  if (name.length > MAX_NAME) return `name: at most ${MAX_NAME} characters`
  if (!TIERS.includes(body.tier as McpTokenTier)) return 'tier: must be read, write or outward'
  const expires = body.expires_in
  if (
    expires !== undefined &&
    (typeof expires !== 'number' || !Number.isInteger(expires) || expires < 60 || expires > MAX_EXPIRES_IN)
  ) {
    return 'expires_in: whole seconds, at least 60'
  }
  return undefined
}

const authBase = '/api/v1/ai/mcp/auth'

function authView(): McpAuthSetting {
  return { mode: state.authMode ?? 'bearer', anonymous_cap: state.anonymousCap }
}

/** As `agent/src/routes/mcpAuthMode.ts`: the same validation and answers. */
function authProblems(body: Record<string, unknown>): string | undefined {
  if (body.mode === 'oidc') {
    return 'mode: "oidc" cannot be set here; it is switched on with the OIDC configuration once its discovery check passes (#262)'
  }
  const extra = Object.keys(body).filter((key) => !['mode', 'anonymous_cap'].includes(key))
  if (extra.length > 0) return `body: unrecognized key(s) ${extra.join(', ')}`
  if (body.mode !== 'bearer' && body.mode !== 'disabled') return 'mode: must be bearer or disabled'
  if (!TIERS.includes(body.anonymous_cap as McpTokenTier)) {
    return 'anonymous_cap: must be read, write or outward'
  }
  return undefined
}

export const mcpTokenHandlers = [
  http.get(authBase, () =>
    HttpResponse.json(authView(), { headers: { 'Cache-Control': 'no-store' } }),
  ),

  http.put(authBase, async ({ request }) => {
    let body: Record<string, unknown>
    try {
      body = (await request.json()) as Record<string, unknown>
    } catch {
      return detail(400, 'body is not valid JSON')
    }
    if (body === null || typeof body !== 'object') return detail(400, 'body: expected an object')
    const problem = authProblems(body)
    if (problem) return detail(400, problem)
    const update = body as unknown as McpAuthUpdate
    state.authMode = update.mode
    state.anonymousCap = update.anonymous_cap
    return HttpResponse.json(authView())
  }),

  http.get(base, () =>
    HttpResponse.json(
      { auth_mode: state.authMode, tokens: state.tokens },
      { headers: { 'Cache-Control': 'no-store' } },
    ),
  ),

  http.post(base, async ({ request }) => {
    let body: Partial<McpTokenCreate> & Record<string, unknown>
    try {
      body = (await request.json()) as typeof body
    } catch {
      return detail(400, 'body is not valid JSON')
    }
    const problem = problems(body)
    if (problem) return detail(400, problem)
    const now = new Date()
    const record: McpToken = {
      id: crypto.randomUUID(),
      name: String(body.name).trim(),
      tier: body.tier as McpTokenTier,
      created_at: now.toISOString(),
      expires_at:
        body.expires_in === undefined
          ? null
          : new Date(now.getTime() + body.expires_in * 1000).toISOString(),
      last_used_at: null,
      revoked_at: null,
      status: 'active',
    }
    state.tokens = [record, ...state.tokens]
    const minted: MintedMcpToken = { token: randomToken(), record }
    return HttpResponse.json(minted, { status: 201, headers: { 'Cache-Control': 'no-store' } })
  }),

  http.delete(`${base}/:id`, ({ params }) => {
    const token = state.tokens.find((t) => t.id === params.id)
    if (!token || token.revoked_at) {
      return detail(404, 'no live token has that id (it is unknown or already revoked)')
    }
    const revoked: McpToken = { ...token, revoked_at: new Date().toISOString(), status: 'revoked' }
    state.tokens = state.tokens.map((t) => (t.id === token.id ? revoked : t))
    return new HttpResponse(null, { status: 204 })
  }),
]
