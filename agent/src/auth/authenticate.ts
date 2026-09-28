import { checkOrigin, isSecureTransport, type OriginPolicy, type RequestFacts } from '../http/origins.js'
import { mcpResourceUri, type OidcConfig, type OidcConfigRepo, type OidcProvider, resourceMetadataUrl } from './oidc.js'
import { type Principal, type Tier, TIERS, tiersUpTo } from './principal.js'
import { TOKEN_PREFIX, type TokenStore } from './tokens.js'

// `/mcp` authentication and transport rules, spec §8.3–§8.4
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md) and issues
// #251 (bearer, disabled) and #262 (oidc).
//
// The mode and the anonymous cap are database settings (spec D4, §8.3, §9: "MCP
// auth mode" is AI state in the `ai_*` tables, and there are "no
// AI-configuration env vars"). They are read on every /mcp request by
// `mcpAuthSettings` below, so a change applies to the next request on every
// replica:
//   - `oidc` is on while the OIDC configuration (`ai_settings.mcp_oidc`,
//     src/auth/oidc.ts, #262) is enabled, whatever the mode key says;
//   - otherwise the `mcp_auth_mode` key picks `bearer` (the default) or
//     `disabled`, and `mcp_anonymous_cap` caps `disabled`.
// An unset key is the default; a value that is not one of the allowed ones
// fails closed.

export type McpAuthMode = 'bearer' | 'disabled' | 'oidc'

export type McpAuthSettings = {
  mode: McpAuthMode
  /**
   * What the `mcp_auth_mode` key alone gives: the mode while OIDC is off. It
   * differs from `mode` while an enabled OIDC configuration overrides it.
   */
  configuredMode?: Exclude<McpAuthMode, 'oidc'> | undefined
  /** The most an `anonymous` caller may do in `disabled` mode. Full access by default (spec §8.3). */
  anonymousCap: Tier
  /** The IdP, in `oidc` mode (#262). */
  oidc?: OidcConfig | undefined
}

/**
 * What `oidc` mode needs besides the settings: the verifier (with its caches)
 * and the public URL the resource URI is made from. Without a public URL there
 * is no resource URI to demand as the audience, so JWTs are refused and only
 * bearer tokens work (Settings refuses to enable OIDC in that state anyway).
 */
export type OidcRuntime = { provider: OidcProvider; publicUrl: string | undefined }

/** The resource, its metadata URL and the audience to demand, when `oidc` mode can run. */
export function oidcContext(
  settings: McpAuthSettings,
  runtime: OidcRuntime | undefined,
): { config: OidcConfig; resource: string; metadataUrl: string; audience: string } | undefined {
  const config = settings.oidc
  if (settings.mode !== 'oidc' || !config?.enabled || !runtime?.publicUrl) return undefined
  const resource = mcpResourceUri(runtime.publicUrl)
  return { config, resource, metadataUrl: resourceMetadataUrl(runtime.publicUrl), audience: config.audience ?? resource }
}

/**
 * A quoted-string value for a challenge parameter (RFC 9110 §5.6.4), limited to
 * the RFC 6750 §3 `error_description` set (%x20-21 / %x23-5B / %x5D-7E). Anything
 * else (`"`, `\`, controls, non-ASCII) would break the header or make it unsendable.
 */
export function quoted(value: string): string {
  return `"${value.replace(/[^\x20\x21\x23-\x5b\x5d-\x7e]/g, (c) => (c === '"' || c === '\\' ? "'" : '?'))}"`
}

export const DEFAULT_MCP_AUTH: McpAuthSettings = {
  mode: 'bearer',
  configuredMode: 'bearer',
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
 * An enabled OIDC configuration (`oidc`, #262) wins over the mode key, even
 * over `disabled`: of two explicit choices the stricter one is kept. A stored
 * `oidc` mode without an enabled configuration is `bearer` (what `oidc` mode
 * would do without one: only bearer tokens verify).
 *
 * A value that is not allowed is not guessed at: an unknown mode is `bearer`
 * and an unknown cap is `read`. `disabled` mode, and each bad or overridden
 * value, is logged through `warn` when it is first seen, and again whenever it
 * changes, rather than on every request.
 */
export function mcpAuthSettings(
  settings: SettingsReader | undefined,
  warn: (message: string) => void,
  oidc?: OidcConfigRepo,
): () => Promise<McpAuthSettings> {
  let lastWarning = ''
  return async () => {
    if (!settings) return DEFAULT_MCP_AUTH
    const [mode, cap, oidcConfig] = await Promise.all([
      settings.get<unknown>(SETTING_MCP_AUTH_MODE),
      settings.get<unknown>(SETTING_MCP_ANONYMOUS_CAP),
      oidc?.get(),
    ])
    const warnings: string[] = []
    const resolved: McpAuthSettings = { ...DEFAULT_MCP_AUTH }
    if (mode === 'disabled') resolved.configuredMode = 'disabled'
    if (oidcConfig?.enabled) {
      resolved.mode = 'oidc'
      resolved.oidc = oidcConfig
      if (mode === 'disabled') {
        warnings.push(`ai_settings ${SETTING_MCP_AUTH_MODE} is "disabled", but OIDC is enabled (ai_settings mcp_oidc); using oidc`)
      }
    } else if (mode === 'oidc') {
      warnings.push(`ai_settings ${SETTING_MCP_AUTH_MODE} is "oidc", but no OIDC configuration is enabled; using bearer`)
    } else if (typeof mode === 'string' && MODES.includes(mode)) resolved.mode = mode as McpAuthMode
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

/**
 * `oidc` mode (#262). A ScadBuddy bearer token (`sbmcp_…`) still goes to the
 * token store; anything else must be a JWT access token from the configured
 * IdP. Every 401 carries `resource_metadata` (RFC 9728 §5.1), which is how an
 * MCP client finds the IdP and starts its own browser login; errors follow
 * RFC 6750 §3.1: `invalid_token` → 401, `insufficient_scope` → 403 naming
 * the scope that is missing. An IdP that cannot be reached is a 503, not a
 * 401, so a client does not throw its token away over an outage.
 */
async function authenticateOidc(
  request: Request,
  deps: { settings: McpAuthSettings; tokens: TokenStore; clientAddress: string | undefined; oidc?: OidcRuntime | undefined },
): Promise<AuthResult> {
  const { settings, tokens, clientAddress } = deps
  const ctx = oidcContext(settings, deps.oidc)
  const challenge = (params: Record<string, string> = {}) =>
    ['Bearer realm="scadbuddy"', ...(ctx ? [`resource_metadata=${quoted(ctx.metadataUrl)}`] : [])]
      .concat(Object.entries(params).map(([k, v]) => `${k}=${quoted(v)}`))
      .join(', ')
  const refuse = (status: number, detail: string, params: Record<string, string> = {}): AuthResult => ({
    ok: false,
    response: problem(status, detail, { 'WWW-Authenticate': challenge(params) }),
  })

  const token = bearerOf(request)
  if (token === undefined) return refuse(401, 'an access token is required')
  if (token.startsWith(TOKEN_PREFIX)) {
    const principal = await tokens.verify(token)
    if (!principal) return refuse(401, 'the bearer token is unknown, expired or revoked', { error: 'invalid_token' })
    return { ok: true, principal: { ...principal, clientIp: clientAddress } }
  }
  if (!ctx) {
    return refuse(
      401,
      'OIDC access tokens cannot be checked: SCADBUDDY_PUBLIC_URL is not set, so this server has no resource URI',
      { error: 'invalid_token' },
    )
  }
  const verdict = await deps.oidc!.provider.verify(token, ctx.config, ctx.audience)
  if (verdict.ok) return { ok: true, principal: { ...verdict.principal, clientIp: clientAddress } }
  switch (verdict.error) {
    case 'invalid_token':
      return refuse(401, verdict.detail, { error: 'invalid_token', error_description: verdict.detail })
    case 'insufficient_scope':
      return refuse(403, verdict.detail, {
        error: 'insufficient_scope',
        scope: TIERS.map((t) => ctx.config.scopes[t]).join(' '),
        error_description: verdict.detail,
      })
    case 'temporarily_unavailable':
      return { ok: false, response: problem(503, verdict.detail, { 'Retry-After': '30' }) }
  }
}

/** `authenticate(request) → principal`, one interface for every mode (spec §8.1, D6). */
export async function authenticate(
  request: Request,
  deps: {
    settings: McpAuthSettings
    tokens: TokenStore
    clientAddress: string | undefined
    oidc?: OidcRuntime | undefined
  },
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
      return authenticateOidc(request, deps)
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
