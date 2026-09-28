import { checkOrigin, isSecureTransport, type OriginPolicy, type RequestFacts } from '../http/origins.js'
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
}

export const DEFAULT_MCP_AUTH: McpAuthSettings = {
  mode: 'bearer',
  anonymousCap: 'outward',
}

export type AuthResult = { ok: true; principal: Principal } | { ok: false; response: Response }

const REALM = 'scadbuddy'

function problem(status: number, detail: string, headers: Record<string, string> = {}): Response {
  return Response.json({ error: detail }, { status, headers })
}

// HTTPS and Origin (spec §8.4) are NOT decided here: /mcp uses the one shared
// allowlist in src/http/origins.ts (#379), through `mcpTransportProblem` below,
// exactly as the credential routes do (routes/guard.ts). That module trusts
// X-Forwarded-* only from SCADBUDDY_AGENT_TRUSTED_PROXIES peers and matches
// Origin against SCADBUDDY_PUBLIC_URL (or the loopback pair from a loopback
// peer), never against the request's own Host, which a DNS-rebinding page
// controls.

/**
 * Refuses a request /mcp must not serve, before any auth:
 *
 * - not HTTPS, in every auth mode including `disabled` (spec §8.4, D5):
 *   `isSecureTransport`, i.e. a trusted proxy says https, or a loopback peer
 *   with no proxy involved (the local-development exception as origins.ts
 *   defines it). 403 naming the https URL.
 * - an `Origin` that is not allowed. MCP clients that are not browsers send
 *   none and pass; a browser page must be the allowlisted origin, so a
 *   rebinding page (Origin equal to its own Host) is refused. 403.
 */
export function mcpTransportProblem(facts: RequestFacts, policy: OriginPolicy, url: string): Response | undefined {
  if (!isSecureTransport(facts, policy)) {
    const path = new URL(url)
    const base = [...policy.publicOrigins][0] ?? `https://${facts.header('host') ?? path.host}`
    const httpsUrl = `${base.replace(/^http:/, 'https:')}${path.pathname}${path.search}`
    return Response.json(
      { error: `MCP is served over HTTPS only; use ${httpsUrl}`, https_url: httpsUrl },
      { status: 403 },
    )
  }
  if (facts.header('origin') !== undefined) {
    const verdict = checkOrigin(facts, policy)
    if (!verdict.ok) {
      return problem(
        403,
        `Origin ${facts.header('origin')} is not allowed to call this endpoint (only the ScadBuddy public ` +
          'URL, SCADBUDDY_PUBLIC_URL, or a loopback origin from a loopback peer)',
      )
    }
  }
  return undefined
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
      // `anonymous` is only the base id: ../mcp/http.ts rebinds it to
      // `anonymous:<sessionId>` so anonymous sessions stay apart, which makes
      // the session id the one thing separating LAN clients in this mode.
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
