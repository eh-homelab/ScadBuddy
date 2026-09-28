import {
  createLocalJWKSet,
  decodeProtectedHeader,
  errors,
  type JSONWebKeySet,
  type JWTPayload,
  jwtVerify,
  type JWTVerifyGetKey,
} from 'jose'
import { z } from 'zod'
import { EgressError, egressGetJson, type Resolver } from '../http/egress.js'
import { type Principal, type Tier, TIERS, tiersUpTo } from './principal.js'

// `/mcp` as an OAuth 2.1 resource server (issue #262; spec §8.3, `oidc` mode).
//
// Specifications followed, with the part each one decides:
//
// - MCP authorization, revision 2025-11-25, the revision the pinned
//   @modelcontextprotocol/sdk (1.30.x, LATEST_PROTOCOL_VERSION '2025-11-25')
//   implements: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
//   The MCP server is a resource server; it MUST implement Protected Resource
//   Metadata, MUST answer a missing or invalid token with 401 and a
//   `WWW-Authenticate` header carrying `resource_metadata`, MUST validate that
//   a token was issued for it as the audience, and MUST NOT pass a token it
//   received on to anything upstream (no token passthrough: nothing here
//   reaches the Python API, which is called without the caller's token).
// - RFC 9728, OAuth 2.0 Protected Resource Metadata:
//   https://www.rfc-editor.org/rfc/rfc9728 (§2 the document, §3 where it is
//   served, §5.1 the `resource_metadata` parameter of `WWW-Authenticate`).
// - RFC 8707, Resource Indicators: https://www.rfc-editor.org/rfc/rfc8707.
//   The client names this server's resource URI when it asks for a token, and
//   the IdP puts it in `aud`; a token for any other audience is refused, so a
//   token issued for some other service cannot be replayed here.
// - RFC 6750, Bearer Token Usage: https://www.rfc-editor.org/rfc/rfc6750
//   (§2.1 the Authorization header, §3 the challenge, §3.1 the error codes
//   `invalid_token` → 401 and `insufficient_scope` → 403).
// - RFC 8414 / OpenID Connect Discovery 1.0 for the issuer's metadata
//   (https://www.rfc-editor.org/rfc/rfc8414, https://openid.net/specs/openid-connect-discovery-1_0.html),
//   probed in the order the MCP authorization spec gives
//   ("Authorization Server Metadata Discovery").
//
// Tokens are JWTs (RFC 9068 profile, https://www.rfc-editor.org/rfc/rfc9068)
// verified with jose against the issuer's JWKS. The JWKS and the metadata are
// fetched by `egressGetJson` (src/http/egress.ts): https only (http for
// loopback only), link-local and cloud-metadata addresses refused on the
// connection itself, no redirects, bounded size and time.

/** The JWS algorithms that may be allowed. `none` and the HMAC family are not among them. */
export const SUPPORTED_ALGORITHMS = [
  'RS256',
  'RS384',
  'RS512',
  'PS256',
  'PS384',
  'PS512',
  'ES256',
  'ES384',
  'ES512',
  'EdDSA',
] as const
export type JwsAlgorithm = (typeof SUPPORTED_ALGORITHMS)[number]

/** What Authentik, Keycloak and Pocket ID sign access tokens with by default. */
export const DEFAULT_ALGORITHMS: JwsAlgorithm[] = ['RS256', 'ES256']

/** The scopes spec §8.1 names for the three tiers. */
export const DEFAULT_SCOPES: Record<Tier, string> = {
  read: 'scadbuddy:read',
  write: 'scadbuddy:write',
  outward: 'scadbuddy:outward',
}

/** RFC 6749 §3.3 scope-token: printable ASCII except space, `"` and `\`. */
const ScopeToken = z.string().regex(/^[\x21\x23-\x5B\x5D-\x7E]{1,128}$/, 'a scope is printable ASCII without spaces or quotes')

/** Where `SettingsOidcConfigRepo` keeps it in `ai_settings`. */
export const OIDC_SETTINGS_KEY = 'mcp_oidc'

/**
 * The OIDC configuration, as stored in `ai_settings` and as the Settings
 * route reads and writes it. Not secret: an issuer URL, an audience and a
 * public client id.
 */
export const OidcConfigSchema = z
  .strictObject({
    /** On: `/mcp` runs in `oidc` mode (bearer tokens keep working). */
    enabled: z.boolean(),
    /** Exactly as the IdP writes it in `iss` and in its metadata's `issuer`. */
    issuer: z.url({ protocol: /^https?$/ }).max(2048),
    /** The `aud` a token must carry. Null means the resource URI (`<SCADBUDDY_PUBLIC_URL>/mcp`), as RFC 8707 has it. */
    audience: z.string().min(1).max(2048).nullable(),
    /** The client id for a future in-app login; not used to validate `/mcp` tokens. */
    client_id: z.string().min(1).max(256).nullable(),
    /** Which scope grants which tier. */
    scopes: z.strictObject({ read: ScopeToken, write: ScopeToken, outward: ScopeToken }),
    /**
     * Another claim whose values also count as scopes, e.g. `groups` when the
     * IdP puts a group rather than a scope in the token. Null: `scope`/`scp` only.
     */
    tier_claim: z
      .string()
      .regex(/^[A-Za-z0-9_.:/-]{1,64}$/)
      .nullable(),
    /** Allowed `alg` values; a token signed with anything else is refused before any key is looked up. */
    algorithms: z.array(z.enum(SUPPORTED_ALGORITHMS)).min(1).max(SUPPORTED_ALGORITHMS.length),
  })
  .refine((c) => new Set(Object.values(c.scopes)).size === 3, {
    message: 'the three tiers need three different scopes',
    path: ['scopes'],
  })
  .refine((c) => new Set(c.algorithms).size === c.algorithms.length, {
    message: 'an algorithm is listed twice',
    path: ['algorithms'],
  })
export type OidcConfig = z.infer<typeof OidcConfigSchema>

export function defaultOidcConfig(issuer = 'https://idp.example.com/'): OidcConfig {
  return {
    enabled: false,
    issuer,
    audience: null,
    client_id: null,
    scopes: { ...DEFAULT_SCOPES },
    tier_claim: null,
    algorithms: [...DEFAULT_ALGORITHMS],
  }
}

/** Where the OIDC configuration is kept. */
export interface OidcConfigRepo {
  get(): Promise<OidcConfig | undefined>
  put(config: OidcConfig): Promise<void>
}

/** The slice of `SettingsStore` (src/credentials.ts) this needs. */
export type SettingsLike = {
  get<T>(key: string): Promise<T | undefined>
  set(key: string, value: unknown): Promise<void>
}

/**
 * The configuration in `ai_settings` under `mcp_oidc` (spec §9: "MCP auth
 * mode, tokens (hashed), and OIDC configuration" live in the database; there
 * are no env vars for it). A stored value that no longer parses reads as
 * absent, i.e. OIDC off, and is reported through `onInvalid`: a hand-edited
 * row turns OIDC off rather than half on.
 */
export class SettingsOidcConfigRepo implements OidcConfigRepo {
  readonly #settings: SettingsLike
  readonly #onInvalid: (detail: string) => void

  constructor(settings: SettingsLike, onInvalid: (detail: string) => void = () => {}) {
    this.#settings = settings
    this.#onInvalid = onInvalid
  }

  async get(): Promise<OidcConfig | undefined> {
    const raw = await this.#settings.get<unknown>(OIDC_SETTINGS_KEY)
    if (raw === undefined || raw === null) return undefined
    const parsed = OidcConfigSchema.safeParse(raw)
    if (!parsed.success) {
      this.#onInvalid(`ai_settings.${OIDC_SETTINGS_KEY} is not a valid OIDC configuration; OIDC is off until it is saved again`)
      return undefined
    }
    return parsed.data
  }

  async put(config: OidcConfig): Promise<void> {
    await this.#settings.set(OIDC_SETTINGS_KEY, OidcConfigSchema.parse(config))
  }
}

// ---------------------------------------------------------------------------
// The resource and its metadata (RFC 9728)

/** The `/mcp` resource URI: the public origin plus `/mcp` (RFC 8707 §2: absolute, no fragment). */
export function mcpResourceUri(publicUrl: string): string {
  return `${new URL(publicUrl).origin}/mcp`
}

/** Where the metadata is served; `WWW-Authenticate` names it (RFC 9728 §5.1). */
export const RESOURCE_METADATA_PATH = '/.well-known/oauth-protected-resource'

export function resourceMetadataUrl(publicUrl: string): string {
  return `${new URL(publicUrl).origin}${RESOURCE_METADATA_PATH}`
}

/** RFC 9728 §2. The shape matches the SDK's `OAuthProtectedResourceMetadataSchema`. */
export function protectedResourceMetadata(config: OidcConfig, publicUrl: string): Record<string, unknown> {
  return {
    resource: mcpResourceUri(publicUrl),
    authorization_servers: [config.issuer],
    scopes_supported: TIERS.map((t) => config.scopes[t]),
    bearer_methods_supported: ['header'],
    resource_signing_alg_values_supported: config.algorithms,
    resource_name: 'ScadBuddy MCP',
  }
}

// ---------------------------------------------------------------------------
// Scopes → tiers

function words(value: unknown): string[] {
  if (typeof value === 'string') return value.split(' ').filter(Boolean)
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === 'string')
  return []
}

/**
 * The highest tier the token's scopes grant, or undefined for none. Scopes
 * are read from `scope` (RFC 9068 §2.2.3, space-delimited), `scp` (Azure AD,
 * Okta: an array or a string) and `tier_claim` when set. As with bearer
 * tokens, a tier includes those below it: `scadbuddy:outward` alone can also
 * read and write.
 */
export function tierFromClaims(payload: JWTPayload, config: OidcConfig): Tier | undefined {
  const granted = new Set([
    ...words(payload['scope']),
    ...words(payload['scp']),
    ...(config.tier_claim ? words(payload[config.tier_claim]) : []),
  ])
  return [...TIERS].reverse().find((tier) => granted.has(config.scopes[tier]))
}

// ---------------------------------------------------------------------------
// Discovery and JWKS, cached

export type IssuerMetadata = {
  issuer: string
  jwks_uri: string
  authorization_endpoint?: string | undefined
  token_endpoint?: string | undefined
  registration_endpoint?: string | undefined
  scopes_supported?: string[] | undefined
  code_challenge_methods_supported?: string[] | undefined
  /** The URL the document came from. */
  source: string
}

const MetadataDoc = z.looseObject({
  issuer: z.string(),
  jwks_uri: z.string(),
  authorization_endpoint: z.string().optional(),
  token_endpoint: z.string().optional(),
  registration_endpoint: z.string().optional(),
  scopes_supported: z.array(z.string()).optional(),
  code_challenge_methods_supported: z.array(z.string()).optional(),
})

const JwksDoc = z.object({ keys: z.array(z.looseObject({ kty: z.string() })) })

/**
 * The metadata URLs to try for `issuer`, in the MCP authorization spec's
 * order: with a path, RFC 8414 path insertion, OIDC path insertion, OIDC path
 * appending; without one, RFC 8414 then OIDC.
 */
export function discoveryUrls(issuer: string): string[] {
  const url = new URL(issuer)
  const path = url.pathname.replace(/\/+$/, '')
  if (path === '') {
    return [`${url.origin}/.well-known/oauth-authorization-server`, `${url.origin}/.well-known/openid-configuration`]
  }
  return [
    `${url.origin}/.well-known/oauth-authorization-server${path}`,
    `${url.origin}/.well-known/openid-configuration${path}`,
    `${url.origin}${path}/.well-known/openid-configuration`,
  ]
}

export type DiscoveryReport = IssuerMetadata & {
  /** How many keys the JWKS holds, and their `alg`s where the IdP names them. */
  keys: number
  key_algorithms: string[]
  /** RFC 7591 dynamic client registration is offered (MCP clients can register themselves). */
  dynamic_registration: boolean
}

export class DiscoveryError extends Error {
  override name = 'DiscoveryError'
}

type Entry<T> = { at: number; value?: T; error?: Error; inflight?: Promise<T> }

export type OidcProviderOptions = {
  /** For the egress check; the system resolver by default. */
  resolve?: Resolver
  /** How long metadata and keys are reused (default 10 min). */
  ttlMs?: number
  /**
   * The least time between two fetches of the same document (default 30 s):
   * a failure is remembered this long, and an unknown `kid` forces a JWKS
   * refetch at most this often, so a flood of forged tokens cannot make the
   * agent hammer the IdP.
   */
  refreshCooldownMs?: number
  /** Per request (default 5 s). */
  timeoutMs?: number
  /** Leeway on `exp`/`nbf`/`iat`, in seconds (default 30). */
  clockToleranceSec?: number
  now?: () => number
}

export type OidcVerdict =
  | { ok: true; principal: Principal }
  | { ok: false; error: 'invalid_token' | 'insufficient_scope' | 'temporarily_unavailable'; detail: string }

function isJwt(token: string): boolean {
  return /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)
}

/** A short reason for a refused token; it goes into `error_description` (RFC 6750 §3). */
function reasonOf(err: unknown): string {
  if (err instanceof errors.JWTExpired) return 'the token has expired'
  if (err instanceof errors.JWTClaimValidationFailed) {
    switch (err.claim) {
      case 'iss':
        return 'the token was issued by another issuer'
      case 'aud':
        return 'the token was issued for another audience (resource)'
      case 'nbf':
        return 'the token is not valid yet'
      default:
        return `the token's "${err.claim}" claim is missing or invalid`
    }
  }
  if (err instanceof errors.JOSEAlgNotAllowed) return 'the token is signed with an algorithm that is not allowed'
  if (err instanceof errors.JWKSNoMatchingKey) return 'no key of the issuer matches the token'
  if (err instanceof errors.JWSSignatureVerificationFailed) return 'the token signature is invalid'
  return 'the token is not a valid JWT'
}

/**
 * The issuer's metadata and keys, cached, and access-token verification on
 * top of them. One instance per process; the cache is keyed by URL, so a
 * changed issuer in Settings just starts new entries (and `forget` drops the
 * old ones).
 */
export class OidcProvider {
  readonly #resolve: Resolver | undefined
  readonly #ttlMs: number
  readonly #cooldownMs: number
  readonly #timeoutMs: number
  readonly #clockTolerance: number
  readonly #now: () => number
  readonly #metadata = new Map<string, Entry<IssuerMetadata>>()
  readonly #jwks = new Map<string, Entry<JSONWebKeySet>>()

  constructor(options: OidcProviderOptions = {}) {
    this.#resolve = options.resolve
    this.#ttlMs = options.ttlMs ?? 10 * 60_000
    this.#cooldownMs = options.refreshCooldownMs ?? 30_000
    this.#timeoutMs = options.timeoutMs ?? 5000
    this.#clockTolerance = options.clockToleranceSec ?? 30
    this.#now = options.now ?? Date.now
  }

  #get(url: string, label: string): Promise<unknown> {
    return egressGetJson(url, {
      label,
      timeoutMs: this.#timeoutMs,
      ...(this.#resolve ? { resolve: this.#resolve } : {}),
    })
  }

  /**
   * The cached value, or a fetch. `fresh` forces a fetch unless one ran
   * within the cooldown. Concurrent callers share one fetch.
   */
  async #cached<T>(cache: Map<string, Entry<T>>, key: string, fresh: boolean, load: () => Promise<T>): Promise<T> {
    const now = this.#now()
    const entry = cache.get(key)
    if (entry?.inflight) return entry.inflight
    if (entry) {
      const age = now - entry.at
      if (entry.error && age < this.#cooldownMs) throw entry.error
      if (entry.value !== undefined && age < this.#ttlMs && (!fresh || age < this.#cooldownMs)) return entry.value
    }
    const inflight = load()
    cache.set(key, { ...entry, at: now, inflight })
    try {
      const value = await inflight
      cache.set(key, { at: this.#now(), value })
      return value
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err))
      // Keep serving the last good value on a failed refresh; remember the failure otherwise.
      cache.set(key, entry?.value !== undefined ? { at: this.#now(), value: entry.value } : { at: this.#now(), error })
      throw error
    }
  }

  async #loadMetadata(issuer: string): Promise<IssuerMetadata> {
    const problems: string[] = []
    for (const url of discoveryUrls(issuer)) {
      let raw: unknown
      try {
        raw = await this.#get(url, 'issuer metadata')
      } catch (err) {
        if (!(err instanceof EgressError)) throw err
        problems.push(err.message)
        // A refused host or scheme is the same for every candidate URL.
        if (!/answered HTTP/.test(err.message)) break
        continue
      }
      const doc = MetadataDoc.safeParse(raw)
      if (!doc.success) {
        problems.push(`${url} is not authorization server metadata (it needs "issuer" and "jwks_uri")`)
        continue
      }
      // RFC 8414 §3.3 and OIDC Discovery §4.3: the issuer in the document MUST
      // be identical to the one it was fetched for.
      if (doc.data.issuer !== issuer) {
        throw new DiscoveryError(
          `${url} names issuer ${JSON.stringify(doc.data.issuer)}, not ${JSON.stringify(issuer)}; ` +
            'the issuer must be entered exactly as the IdP writes it (mind the trailing slash)',
        )
      }
      return { ...doc.data, source: url }
    }
    throw new DiscoveryError(`no metadata found for issuer ${issuer}: ${problems.join('; ')}`)
  }

  async #loadJwks(uri: string): Promise<JSONWebKeySet> {
    const raw = await this.#get(uri, 'jwks_uri')
    const doc = JwksDoc.safeParse(raw)
    if (!doc.success) throw new DiscoveryError(`${uri} is not a JWK Set (RFC 7517 §5: an object with "keys")`)
    // Only public signing keys: a private or symmetric key published by mistake is dropped.
    const keys = doc.data.keys.filter(
      (k) => k['kty'] !== 'oct' && k['d'] === undefined && (k['use'] === undefined || k['use'] === 'sig'),
    )
    return { keys } as JSONWebKeySet
  }

  discover(issuer: string, fresh = false): Promise<IssuerMetadata> {
    return this.#cached(this.#metadata, issuer, fresh, () => this.#loadMetadata(issuer))
  }

  jwks(uri: string, fresh = false): Promise<JSONWebKeySet> {
    return this.#cached(this.#jwks, uri, fresh, () => this.#loadJwks(uri))
  }

  /** Drops everything cached for `issuer`, e.g. after Settings saved it. */
  forget(issuer: string): void {
    const meta = this.#metadata.get(issuer)?.value
    this.#metadata.delete(issuer)
    if (meta) this.#jwks.delete(meta.jwks_uri)
  }

  /**
   * The discovery check Settings runs before `oidc` can be switched on
   * (issue #262: "a typo can't lock out every client"): the metadata and the
   * JWKS, both fetched now, not from the cache.
   */
  async test(issuer: string): Promise<DiscoveryReport> {
    this.forget(issuer)
    const meta = await this.#loadMetadata(issuer)
    const jwks = await this.#loadJwks(meta.jwks_uri)
    if (jwks.keys.length === 0) throw new DiscoveryError(`${meta.jwks_uri} holds no public signing key`)
    return {
      ...meta,
      keys: jwks.keys.length,
      key_algorithms: [...new Set(jwks.keys.map((k) => k.alg).filter((a): a is string => typeof a === 'string'))],
      dynamic_registration: typeof meta.registration_endpoint === 'string',
    }
  }

  /**
   * Verifies an access token: signature by a key of the issuer's JWKS, `alg`
   * on the allowlist, `iss` equal to the issuer, `aud` containing `audience`,
   * `exp` present and in the future (`nbf`/`iat` checked when present), `sub`
   * present, and at least one scope that maps to a tier.
   */
  async verify(token: string, config: OidcConfig, audience: string): Promise<OidcVerdict> {
    if (!isJwt(token)) return { ok: false, error: 'invalid_token', detail: 'the token is not a JWT' }
    let alg: string | undefined
    let typ: string | undefined
    try {
      ;({ alg, typ } = decodeProtectedHeader(token))
    } catch {
      return { ok: false, error: 'invalid_token', detail: 'the token header is not valid' }
    }
    // Checked before any key is fetched, so `none`, HS256 and friends cost nothing.
    if (!alg || !(config.algorithms as string[]).includes(alg)) {
      return { ok: false, error: 'invalid_token', detail: `the token is signed with ${alg ?? 'no algorithm'}, which is not allowed` }
    }
    // RFC 9068 §2.1 says `at+jwt`; many IdPs write `JWT`. An ID token or
    // anything else explicitly typed is not an access token.
    if (typ !== undefined && !['at+jwt', 'application/at+jwt', 'jwt'].includes(typ.toLowerCase())) {
      return { ok: false, error: 'invalid_token', detail: `a token of type ${typ} is not an access token` }
    }

    let jwksUri: string
    try {
      jwksUri = (await this.discover(config.issuer)).jwks_uri
    } catch (err) {
      return { ok: false, error: 'temporarily_unavailable', detail: `the identity provider cannot be reached: ${(err as Error).message}` }
    }
    const getKey: JWTVerifyGetKey = async (header, jws) => {
      try {
        return await createLocalJWKSet(await this.jwks(jwksUri))(header, jws)
      } catch (err) {
        // The IdP may have rotated its keys: fetch again (at most once per cooldown).
        if (!(err instanceof errors.JWKSNoMatchingKey)) throw err
        return createLocalJWKSet(await this.jwks(jwksUri, true))(header, jws)
      }
    }

    let payload: JWTPayload
    try {
      ;({ payload } = await jwtVerify(token, getKey, {
        issuer: config.issuer,
        audience,
        algorithms: config.algorithms,
        clockTolerance: this.#clockTolerance,
        requiredClaims: ['exp', 'sub'],
        currentDate: new Date(this.#now()),
      }))
    } catch (err) {
      if (err instanceof EgressError || err instanceof DiscoveryError) {
        return { ok: false, error: 'temporarily_unavailable', detail: `the identity provider's keys cannot be fetched: ${err.message}` }
      }
      return { ok: false, error: 'invalid_token', detail: reasonOf(err) }
    }

    const sub = payload.sub
    if (typeof sub !== 'string' || sub.length === 0 || sub.length > 255) {
      return { ok: false, error: 'invalid_token', detail: 'the token has no usable "sub"' }
    }
    const tier = tierFromClaims(payload, config)
    if (!tier) {
      return {
        ok: false,
        error: 'insufficient_scope',
        detail: `the token grants none of the scopes ${TIERS.map((t) => config.scopes[t]).join(', ')}`,
      }
    }
    const clientId = typeof payload['azp'] === 'string' ? payload['azp'] : typeof payload['client_id'] === 'string' ? payload['client_id'] : undefined
    return {
      ok: true,
      principal: { id: `oidc:${sub}`, kind: 'oidc', tiers: tiersUpTo(tier), subject: sub, ...(clientId ? { clientId } : {}) },
    }
  }
}
