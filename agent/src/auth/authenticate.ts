import { checkOrigin, isSecureTransport, type OriginPolicy, type RequestFacts } from '../http/origins.js'
import { type Principal, type Tier, TIERS, tiersUpTo } from './principal.js'
import type { TokenStore } from './tokens.js'

// `/mcp` authentication and transport rules, spec §8.3–§8.4
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md) and issue #251.
//
// The mode and the anonymous cap are database settings (spec D4, §8.3, §9: "MCP
// auth mode" is AI state in the `ai_*` tables, and there are "no
// AI-configuration env vars"). They are two keys of `ai_settings`
// (db/migrations/20260927T2349Z_credentials_settings.sql, one JSON value per
// key), read on every /mcp request by `mcpAuthSettings` below, so a change
// applies to the next request on every replica. An unset key is the default;
// a value that is not one of the allowed ones fails closed.

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

/** ai_settings key: `"bearer"` (the default), `"disabled"` or `"oidc"`. */
export const SETTING_MCP_AUTH_MODE = 'mcp_auth_mode'
/** ai_settings key: `"read"`, `"write"` or `"outward"` (the default), the anonymous cap in `disabled` mode. */
export const SETTING_MCP_ANONYMOUS_CAP = 'mcp_anonymous_cap'

const MODES: readonly string[] = ['bearer', 'disabled', 'oidc'] satisfies McpAuthMode[]

/** Reads ai_settings; credentials.ts SettingsStore is one. */
export type SettingsReader = { get<T>(key: string): Promise<T | undefined> }

/**
 * The `authSettings` reader /mcp calls per request (mcp/http.ts), over
 * `ai_settings`; the defaults when there is no settings store. A read that
 * throws is left to throw: mcp/http.ts `resolveAuth` then fails closed
 * (`bearer` with no token that verifies).
 *
 * A value that is not allowed is not guessed at: an unknown mode is `bearer`
 * and an unknown cap is `read`. `disabled` mode, and each bad value, is
 * logged through `warn` when it is first seen, and again whenever it changes,
 * rather than on every request.
 */
export function mcpAuthSettings(
  settings: SettingsReader | undefined,
  warn: (message: string) => void,
): () => Promise<McpAuthSettings> {
  let lastWarning = ''
  return async () => {
    if (!settings) return DEFAULT_MCP_AUTH
    const [mode, cap] = await Promise.all([
      settings.get<unknown>(SETTING_MCP_AUTH_MODE),
      settings.get<unknown>(SETTING_MCP_ANONYMOUS_CAP),
    ])
    const warnings: string[] = []
    const resolved: McpAuthSettings = { ...DEFAULT_MCP_AUTH }
    if (typeof mode === 'string' && MODES.includes(mode)) resolved.mode = mode as McpAuthMode
    else if (mode !== undefined) {
      warnings.push(`ai_settings ${SETTING_MCP_AUTH_MODE} is ${JSON.stringify(mode)}, not one of ${MODES.join(', ')}; using bearer`)
    }
    if (typeof cap === 'string' && (TIERS as readonly string[]).includes(cap)) resolved.anonymousCap = cap as Tier
    else if (cap !== undefined) {
      resolved.anonymousCap = 'read'
      warnings.push(`ai_settings ${SETTING_MCP_ANONYMOUS_CAP} is ${JSON.stringify(cap)}, not one of ${TIERS.join(', ')}; using read`)
    }
    if (resolved.mode === 'disabled') {
      warnings.push(
        `MCP auth is DISABLED (ai_settings ${SETTING_MCP_AUTH_MODE}): /mcp serves any HTTPS caller that can reach it ` +
          `as "anonymous", up to the "${resolved.anonymousCap}" tier (spec §8.3). Outward actions still need a human approval.`,
      )
    }
    const warning = warnings.join('\n')
    if (warning !== lastWarning) {
      lastWarning = warning
      for (const w of warnings) warn(w)
    }
    return resolved
  }
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
