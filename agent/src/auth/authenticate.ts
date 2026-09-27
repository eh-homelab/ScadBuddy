import { type Principal, type Tier, tiersUpTo } from './principal.js'
import type { TokenStore } from './tokens.js'

// `/mcp` authentication and transport rules, spec §8.3–§8.4
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md) and issue #251.
//
// The mode and the anonymous cap are database settings edited in Settings
// (spec D4, §8.3). That storage arrives with #255; until then they are plain
// config handed to `createApp`, and `main.ts` passes `DEFAULT_MCP_AUTH`.

export type McpAuthMode = 'bearer' | 'disabled' | 'oidc'

export type McpAuthSettings = {
  mode: McpAuthMode
  /** The most an `anonymous` caller may do in `disabled` mode. Full access by default (spec §8.3). */
  anonymousCap: Tier
  /**
   * Extra `Origin` values allowed besides the endpoint's own origin, e.g. a
   * browser-based MCP client on another host. Empty by default.
   */
  allowedOrigins: readonly string[]
}

export const DEFAULT_MCP_AUTH: McpAuthSettings = {
  mode: 'bearer',
  anonymousCap: 'outward',
  allowedOrigins: [],
}

export type AuthResult = { ok: true; principal: Principal } | { ok: false; response: Response }

const REALM = 'scadbuddy'

function problem(status: number, detail: string, headers: Record<string, string> = {}): Response {
  return Response.json({ error: detail }, { status, headers })
}

export function isLoopback(address: string | undefined): boolean {
  if (!address) return false
  const bare = address.startsWith('::ffff:') ? address.slice('::ffff:'.length) : address
  return bare === '::1' || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(bare)
}

/** The first value of a possibly comma-joined forwarded header, lower-cased. */
function firstForwarded(value: string | null): string | undefined {
  const first = value?.split(',')[0]?.trim().toLowerCase()
  return first ? first : undefined
}

function hostOf(request: Request): string {
  return request.headers.get('host') ?? new URL(request.url).host
}

/**
 * HTTPS in every auth mode, `disabled` included (spec §8.4, D5).
 *
 * TLS ends at the cluster ingress, which sets `X-Forwarded-Proto`. The header
 * is trusted because the agent's port is reachable only through that ingress
 * and the pod's own loopback (spec §4.2–§4.3): anyone who can reach the port
 * directly, bypassing the ingress, could forge it. A deployment that exposes
 * port 8081 some other way breaks that assumption.
 *
 * Allowed: `X-Forwarded-Proto: https`; or no forwarded header at all on a
 * loopback connection (local development and tests). Everything else,
 * including a loopback proxy that says `http`, gets 403 naming the HTTPS URL.
 */
export function checkTransport(request: Request, clientAddress: string | undefined): Response | undefined {
  const forwarded = firstForwarded(request.headers.get('x-forwarded-proto'))
  if (forwarded === 'https') return undefined
  if (forwarded === undefined && isLoopback(clientAddress)) return undefined
  const url = new URL(request.url)
  const httpsUrl = `https://${hostOf(request)}${url.pathname}${url.search}`
  return Response.json(
    { error: `MCP is served over HTTPS only; use ${httpsUrl}`, https_url: httpsUrl },
    { status: 403 },
  )
}

/**
 * DNS-rebinding guard (spec §8.4). Non-browser MCP clients send no `Origin`
 * and pass; a browser's `Origin` must be this endpoint's own origin or one
 * the operator allowed.
 */
export function checkOrigin(
  request: Request,
  clientAddress: string | undefined,
  settings: McpAuthSettings,
): Response | undefined {
  const origin = request.headers.get('origin')
  if (origin === null) return undefined
  const host = hostOf(request)
  const own = new Set([`https://${host}`])
  // Plain HTTP is only ever accepted on loopback, so only there is http:// our origin.
  if (isLoopback(clientAddress) && request.headers.get('x-forwarded-proto') === null) {
    own.add(`http://${host}`)
  }
  if (own.has(origin) || settings.allowedOrigins.includes(origin)) return undefined
  return problem(403, `Origin ${origin} is not allowed to call this endpoint`)
}

function bearerOf(request: Request): string | undefined {
  const header = request.headers.get('authorization')
  const match = header ? /^Bearer\s+(\S+)\s*$/i.exec(header) : null
  return match?.[1]
}

/** `authenticate(request) → principal`, one interface for every mode (spec §8.1, D6). */
export async function authenticate(
  request: Request,
  deps: { settings: McpAuthSettings; tokens: TokenStore; clientAddress: string | undefined },
): Promise<AuthResult> {
  const { settings, tokens, clientAddress } = deps
  switch (settings.mode) {
    case 'disabled':
      // The operator chose to trust the network (spec §8.3). Outward tools
      // still stop at the approval gate, which is independent of auth.
      return {
        ok: true,
        principal: {
          id: 'anonymous',
          kind: 'anonymous',
          tiers: tiersUpTo(settings.anonymousCap),
          clientIp: clientAddress,
        },
      }
    case 'oidc':
      return {
        ok: false,
        response: problem(
          501,
          'MCP auth mode "oidc" is not implemented yet (#262); switch the mode to "bearer" or "disabled" in Settings',
        ),
      }
    case 'bearer': {
      const token = bearerOf(request)
      if (token === undefined) {
        return {
          ok: false,
          response: problem(401, 'a bearer token is required', {
            'WWW-Authenticate': `Bearer realm="${REALM}"`,
          }),
        }
      }
      const principal = await tokens.verify(token)
      if (!principal) {
        return {
          ok: false,
          response: problem(401, 'the bearer token is unknown, expired or revoked', {
            'WWW-Authenticate': `Bearer realm="${REALM}", error="invalid_token"`,
          }),
        }
      }
      return { ok: true, principal: { ...principal, clientIp: clientAddress } }
    }
  }
}
