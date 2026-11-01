import { createHash, randomBytes } from 'node:crypto'
import type { IncomingHttpHeaders } from 'node:http'
import { assertHostAllowed, assertSecureUrl, EgressError, type Resolver, systemResolver } from '../http/egress.js'
import { PinnedRequestError, pinnedFetch } from '../http/pinned.js'
import { assertEndpointAllowed } from './registry.js'

// OAuth 2.1 for remote MCP plugins (#780): the protocol half. The store and the flow's
// rows are oauthStore.ts; the routes are routes/plugins.ts (start, sign out) and
// routes/pluginOAuth.ts (the browser's authorize link and the callback).
//
// Followed, with the part each decides:
//
// - MCP authorization, revision 2026-07-28 (the current one):
//   https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/index.md
//   and its pages authorization-server-discovery.md, client-registration.md and
//   security-considerations.md beside it. The client "MUST use OAuth 2.0 Protected Resource
//   Metadata for authorization server discovery"; uses the `resource_metadata` of a 401's
//   `WWW-Authenticate` when present, "otherwise ... MUST fall back to constructing and
//   requesting the well-known URIs" (`discover`); takes the challenge's `scope`, else the
//   metadata's `scopes_supported` (Scope Selection Strategy); "MUST verify PKCE support"
//   from `code_challenge_methods_supported` and refuses without it, using S256; sends
//   `resource` (RFC 8707) "in both authorization requests and token requests"; MUST reject
//   AS metadata whose `issuer` is not the one the URL was built from; records the issuer
//   with the verifier and checks a returned `iss` (RFC 9207, `checkIssuer`); and prefers a
//   pre-registered client, then (no Client ID Metadata Document here: the LAN host is not
//   reachable by an authorization server) Dynamic Client Registration, with
//   `application_type: "web"` (client-registration.md "Dynamic Client Registration").
// - RFC 9728 (https://www.rfc-editor.org/rfc/rfc9728): §3.1 the well-known path insertion,
//   §3.3 and §7.3 the `resource` check (`resourceMatches`), §5.1 the challenge parameter.
// - RFC 8414 (https://www.rfc-editor.org/rfc/rfc8414): §3.1 the metadata URL, §3.3 the
//   issuer check, §2 `token_endpoint_auth_methods_supported` (default client_secret_basic).
// - RFC 7591 (https://www.rfc-editor.org/rfc/rfc7591) §3.1–3.2: the registration request.
// - RFC 7636 (https://www.rfc-editor.org/rfc/rfc7636) §4.1–4.2: verifier and S256 challenge.
// - RFC 6749 §2.3.1 (https://www.rfc-editor.org/rfc/rfc6749#section-2.3.1): client
//   authentication, one method per request; §5.2 the error response.
//
// EGRESS. Every URL fetched here passes the egress check (http/egress.ts) and is fetched at
// the address that check passed, with no redirect followed (http/pinned.ts). The plugin's own
// URL and its metadata follow the plugin's rule (registry.ts `assertEndpointAllowed`: https,
// or http to a private address); the authorization server's endpoints must be https (the
// spec's Communication Security: "All authorization server endpoints MUST be served over
// HTTPS"), loopback http aside for tests. Nothing here sends a token anywhere but the token
// endpoint, and no error message carries one.

export const OAUTH_TIMEOUT_MS = 10_000
const MAX_BYTES = 256 * 1024

/** A refusal the person can act on; `status` is the route's answer. */
export class OAuthError extends Error {
  override name = 'OAuthError'
  readonly status: 400 | 409 | 502
  constructor(message: string, status: 400 | 409 | 502 = 502) {
    super(message)
    this.status = status
  }
}

/**
 * The token endpoint refused (an RFC 6749 §5.2 error response): the grant is no good and
 * the person must sign in again. `code` is the server's `error`.
 */
export class TokenRefusedError extends OAuthError {
  override name = 'TokenRefusedError'
  readonly code: string
  constructor(code: string, description: string | undefined) {
    super(`the authorization server refused: ${code}${description ? ` (${description})` : ''}`, 409)
    this.code = code
  }
}

/** `error` codes that say "try again later", not "sign in again" (RFC 6749 §4.1.2.1). */
const TRANSIENT_ERRORS = new Set(['temporarily_unavailable', 'server_error', 'slow_down'])

/** Server text shown to a person: no control characters, at most 200 characters. */
export function safeText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  // eslint-disable-next-line no-control-regex
  const text = value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim()
  return text ? text.slice(0, 200) : undefined
}

// --------------------------------------------------------------- challenge

export type Challenge = { resourceMetadata?: string; scope?: string; error?: string }

/**
 * The `Bearer` challenge's parameters (RFC 6750 §3, RFC 9728 §5.1), from one or more
 * `WWW-Authenticate` values. Undefined when no value has a Bearer challenge.
 */
export function parseBearerChallenge(header: string | string[] | undefined): Challenge | undefined {
  const values = header === undefined ? [] : Array.isArray(header) ? header : [header]
  for (const value of values) {
    const params = bearerParams(value)
    if (!params) continue
    const out: Challenge = {}
    if (params.resource_metadata) out.resourceMetadata = params.resource_metadata
    if (params.scope) out.scope = params.scope
    if (params.error) out.error = params.error
    return out
  }
  return undefined
}

/** The auth-params of the Bearer challenge in one header value, or undefined. */
function bearerParams(value: string): Record<string, string> | undefined {
  let at = 0
  const n = value.length
  const skip = () => {
    while (at < n && (value[at] === ' ' || value[at] === '\t' || value[at] === ',')) at++
  }
  const token = () => {
    const start = at
    while (at < n && /[A-Za-z0-9!#$%&'*+.^_`|~-]/.test(value[at]!)) at++
    return value.slice(start, at)
  }
  let inBearer = false
  let found: Record<string, string> | undefined
  while (at < n) {
    skip()
    const start = at
    const name = token()
    if (!name) {
      at++
      continue
    }
    let look = at
    while (look < n && (value[look] === ' ' || value[look] === '\t')) look++
    if (value[look] === '=' && value[look + 1] !== '=') {
      // An auth-param of the current challenge.
      at = look + 1
      while (at < n && (value[at] === ' ' || value[at] === '\t')) at++
      let param: string
      if (value[at] === '"') {
        at++
        param = ''
        while (at < n && value[at] !== '"') {
          if (value[at] === '\\' && at + 1 < n) at++
          param += value[at]
          at++
        }
        at++
      } else {
        const begin = at
        while (at < n && value[at] !== ',' && value[at] !== ' ') at++
        param = value.slice(begin, at)
      }
      if (inBearer && found) found[name.toLowerCase()] = param
      continue
    }
    // A scheme name (or a token68 after one, which no Bearer parameter is).
    if (at === start) at++
    if (found && inBearer) return found
    inBearer = name.toLowerCase() === 'bearer'
    if (inBearer) found = {}
  }
  return found
}

// ---------------------------------------------------------------- fetching

type Fetched = { status: number; body: string; headers: IncomingHttpHeaders }

/** One request to `url` at an address its egress check passed. Failures are OAuthError. */
async function fetchChecked(
  url: string,
  rule: 'plugin' | 'server',
  resolve: Resolver,
  label: string,
  init: { method: string; headers?: Record<string, string>; body?: string },
): Promise<Fetched> {
  let address: string | undefined
  try {
    if (rule === 'server') {
      assertSecureUrl(url, label)
      ;[address] = await assertHostAllowed(url, resolve, label, 'an authorization server')
    } else {
      ;[address] = await assertEndpointAllowed(url, resolve)
    }
  } catch (err) {
    if (err instanceof EgressError) throw new OAuthError(err.message, 400)
    throw err
  }
  if (address === undefined) throw new OAuthError(`${label} ${url} resolves to no address`, 400)
  try {
    return await pinnedFetch(url, address, {
      ...init,
      signal: AbortSignal.timeout(OAUTH_TIMEOUT_MS),
      maxBytes: MAX_BYTES,
    })
  } catch (err) {
    const why = err instanceof PinnedRequestError ? err.message : err instanceof Error ? err.message : String(err)
    throw new OAuthError(`${label} ${url} could not be fetched: ${why}`)
  }
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function strings(value: unknown): string[] | undefined {
  return Array.isArray(value) && value.every((v) => typeof v === 'string') ? (value as string[]) : undefined
}

// ---------------------------------------------------------------- discovery

/** The RFC 9728 well-known URLs for `resource`, path-inserted first (the spec's order). */
export function resourceMetadataUrls(resource: string): string[] {
  const url = new URL(resource)
  const path = url.pathname.replace(/\/+$/, '')
  const root = `${url.origin}/.well-known/oauth-protected-resource`
  return path ? [`${root}${path}`, root] : [root]
}

/** The RFC 8414 / OIDC Discovery URLs for `issuer`, in the spec's order. */
export function serverMetadataUrls(issuer: string): string[] {
  const url = new URL(issuer)
  const path = url.pathname.replace(/\/+$/, '')
  if (!path) {
    return [`${url.origin}/.well-known/oauth-authorization-server`, `${url.origin}/.well-known/openid-configuration`]
  }
  return [
    `${url.origin}/.well-known/oauth-authorization-server${path}`,
    `${url.origin}/.well-known/openid-configuration${path}`,
    `${url.origin}${path}/.well-known/openid-configuration`,
  ]
}

/**
 * Whether the metadata's `resource` names the plugin's endpoint (RFC 9728 §3.3, §7.3). The
 * one difference allowed is a trailing slash: GitHub's endpoint is served at `…/mcp/` and its
 * metadata names `…/mcp` (measured 2026-11-01 with curl against
 * https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp).
 */
export function resourceMatches(resource: string, pluginUrl: string): boolean {
  let a: URL
  let b: URL
  try {
    a = new URL(resource)
    b = new URL(pluginUrl)
  } catch {
    return false
  }
  if (a.search || a.hash) return false
  const strip = (u: URL) => `${u.origin}${u.pathname.replace(/\/$/, '')}`
  return strip(a) === strip(b)
}

export type ServerMetadata = {
  issuer: string
  authorization_endpoint: string
  token_endpoint: string
  registration_endpoint?: string
  token_endpoint_auth_methods_supported?: string[]
  authorization_response_iss_parameter_supported?: boolean
}

export type Discovery = {
  /** The canonical resource: the metadata's `resource`, sent as RFC 8707 `resource`. */
  resource: string
  server: ServerMetadata
  /** The scope to ask for, or undefined to send none. */
  scope: string | undefined
}

/**
 * Discovery as the spec's sequence gives it: an unauthenticated MCP request to the plugin
 * (at the address its check passed), the protected-resource metadata it names or the
 * well-known fallback, then the first listed authorization server whose metadata validates.
 */
export async function discover(pluginUrl: string, resolve: Resolver = systemResolver): Promise<Discovery> {
  const probe = await fetchChecked(pluginUrl, 'plugin', resolve, 'url', {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'ScadBuddy', version: '1' } },
    }),
  })
  const challenge = probe.status === 401 ? parseBearerChallenge(probe.headers['www-authenticate']) : undefined
  const candidates = challenge?.resourceMetadata ? [challenge.resourceMetadata] : resourceMetadataUrls(pluginUrl)
  let metadata: Record<string, unknown> | undefined
  for (const url of candidates) {
    const got = await fetchChecked(url, 'plugin', resolve, 'resource_metadata', {
      method: 'GET',
      headers: { accept: 'application/json' },
    })
    const parsed = got.status === 200 ? parseJson(got.body) : undefined
    if (isRecord(parsed)) {
      metadata = parsed
      break
    }
  }
  if (!metadata) {
    throw new OAuthError(
      probe.status === 401
        ? 'the plugin endpoint asks for a credential but publishes no OAuth protected-resource metadata; give it a header instead'
        : `the plugin endpoint does not ask for OAuth (it answered HTTP ${probe.status}, and publishes no protected-resource metadata)`,
      409,
    )
  }
  const resource = metadata.resource
  if (typeof resource !== 'string' || !resourceMatches(resource, pluginUrl)) {
    throw new OAuthError(
      `the protected-resource metadata names resource ${JSON.stringify(resource)}, not the plugin's endpoint ${pluginUrl} (RFC 9728 §3.3)`,
      409,
    )
  }
  const servers = strings(metadata.authorization_servers)
  if (!servers || servers.length === 0) {
    throw new OAuthError('the protected-resource metadata lists no authorization_servers', 409)
  }
  const problems: string[] = []
  for (const issuer of servers) {
    try {
      const server = await serverMetadata(issuer, resolve)
      const supported = strings(metadata.scopes_supported)
      const scope = challenge?.scope ?? (supported && supported.length > 0 ? supported.join(' ') : undefined)
      return { resource, server, scope }
    } catch (err) {
      if (!(err instanceof OAuthError)) throw err
      problems.push(err.message)
    }
  }
  throw new OAuthError(`no authorization server could be used: ${problems.join('; ')}`, 409)
}

/** The issuer's metadata, from the first well-known URL that answers, validated. */
export async function serverMetadata(issuer: string, resolve: Resolver = systemResolver): Promise<ServerMetadata> {
  try {
    assertSecureUrl(issuer, 'issuer')
  } catch (err) {
    if (err instanceof EgressError) throw new OAuthError(err.message, 409)
    throw err
  }
  for (const url of serverMetadataUrls(issuer)) {
    const got = await fetchChecked(url, 'server', resolve, 'authorization server metadata', {
      method: 'GET',
      headers: { accept: 'application/json' },
    })
    if (got.status !== 200) continue
    const doc = parseJson(got.body)
    if (!isRecord(doc)) continue
    // RFC 8414 §3.3: identical, or the metadata MUST NOT be used.
    if (doc.issuer !== issuer) {
      throw new OAuthError(`authorization server metadata at ${url} names issuer ${JSON.stringify(doc.issuer)}, not ${issuer}`, 409)
    }
    const methods = strings(doc.code_challenge_methods_supported)
    if (!methods?.includes('S256')) {
      throw new OAuthError(`authorization server ${issuer} does not offer PKCE with S256 (code_challenge_methods_supported)`, 409)
    }
    const endpoint = (key: string): string => {
      const value = doc[key]
      if (typeof value !== 'string') throw new OAuthError(`authorization server ${issuer} metadata has no ${key}`, 409)
      try {
        assertSecureUrl(value, key)
      } catch (err) {
        if (err instanceof EgressError) throw new OAuthError(err.message, 409)
        throw err
      }
      return value
    }
    const out: ServerMetadata = {
      issuer,
      authorization_endpoint: endpoint('authorization_endpoint'),
      token_endpoint: endpoint('token_endpoint'),
    }
    if (typeof doc.registration_endpoint === 'string') out.registration_endpoint = endpoint('registration_endpoint')
    const auth = strings(doc.token_endpoint_auth_methods_supported)
    if (auth) out.token_endpoint_auth_methods_supported = auth
    if (doc.authorization_response_iss_parameter_supported === true) out.authorization_response_iss_parameter_supported = true
    return out
  }
  throw new OAuthError(`authorization server ${issuer} publishes no metadata at its well-known URLs`, 409)
}

// ------------------------------------------------------------------- client

export type AuthMethod = 'none' | 'client_secret_post' | 'client_secret_basic'

export type Client = {
  clientId: string
  clientSecret?: string
  authMethod: AuthMethod
  registration: 'dynamic' | 'configured'
}

/**
 * A configured client's authentication method. With a secret: `client_secret_post` when the
 * server lists it or lists nothing. RFC 8414's default for an absent list is
 * client_secret_basic, but GitHub publishes no list and documents `client_secret` as a POST
 * parameter (https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#2-users-are-redirected-back-to-your-site-by-github).
 */
export function configuredAuthMethod(server: ServerMetadata, secret: string | undefined): AuthMethod {
  if (!secret) return 'none'
  const listed = server.token_endpoint_auth_methods_supported
  if (!listed || listed.includes('client_secret_post')) return 'client_secret_post'
  if (listed.includes('client_secret_basic')) return 'client_secret_basic'
  throw new OAuthError(
    `authorization server ${server.issuer} accepts neither client_secret_post nor client_secret_basic`,
    409,
  )
}

/** RFC 7591 registration of a public web client for `redirectUri`. */
export async function registerClient(
  server: ServerMetadata,
  redirectUri: string,
  resolve: Resolver = systemResolver,
): Promise<Client> {
  if (!server.registration_endpoint) {
    throw new OAuthError(
      `authorization server ${server.issuer} offers no dynamic client registration: register an OAuth app there ` +
        `with the callback ${redirectUri} and give its client id (and secret)`,
      409,
    )
  }
  const got = await fetchChecked(server.registration_endpoint, 'server', resolve, 'registration_endpoint', {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify({
      client_name: 'ScadBuddy',
      redirect_uris: [redirectUri],
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
      application_type: 'web',
    }),
  })
  const doc = parseJson(got.body)
  if ((got.status !== 201 && got.status !== 200) || !isRecord(doc) || typeof doc.client_id !== 'string') {
    const why = isRecord(doc) ? safeText(doc.error_description) ?? safeText(doc.error) : undefined
    throw new OAuthError(`dynamic client registration at ${server.issuer} failed (HTTP ${got.status}${why ? `: ${why}` : ''})`, 502)
  }
  const secret = typeof doc.client_secret === 'string' && doc.client_secret ? doc.client_secret : undefined
  const method = typeof doc.token_endpoint_auth_method === 'string' ? doc.token_endpoint_auth_method : secret ? 'client_secret_basic' : 'none'
  if (method !== 'none' && method !== 'client_secret_post' && method !== 'client_secret_basic') {
    throw new OAuthError(`dynamic client registration gave token_endpoint_auth_method ${method}, which ScadBuddy cannot use`, 502)
  }
  return { clientId: doc.client_id, ...(secret ? { clientSecret: secret } : {}), authMethod: method, registration: 'dynamic' }
}

// --------------------------------------------------------------------- PKCE

export function randomToken(bytes = 32): string {
  return randomBytes(bytes).toString('base64url')
}

/** RFC 7636 §4.2 S256. */
export function codeChallenge(verifier: string): string {
  return createHash('sha256').update(verifier).digest('base64url')
}

export function sha256(text: string): string {
  return createHash('sha256').update(text).digest('hex')
}

export type AuthorizationRequest = {
  server: Pick<ServerMetadata, 'authorization_endpoint'>
  clientId: string
  redirectUri: string
  state: string
  verifier: string
  resource: string
  scope: string | undefined
}

export function authorizationUrl(r: AuthorizationRequest): string {
  const url = new URL(r.server.authorization_endpoint)
  url.searchParams.set('response_type', 'code')
  url.searchParams.set('client_id', r.clientId)
  url.searchParams.set('redirect_uri', r.redirectUri)
  url.searchParams.set('state', r.state)
  url.searchParams.set('code_challenge', codeChallenge(r.verifier))
  url.searchParams.set('code_challenge_method', 'S256')
  url.searchParams.set('resource', r.resource)
  if (r.scope) url.searchParams.set('scope', r.scope)
  return url.toString()
}

/**
 * RFC 9207 §2.4 as the MCP spec's table applies it: a present `iss` must equal the recorded
 * issuer (simple string comparison, no normalising); an absent one is refused only when the
 * server advertised that it sends it.
 */
export function checkIssuer(recorded: string, advertised: boolean, iss: string | undefined): boolean {
  if (iss !== undefined) return iss === recorded
  return !advertised
}

// ------------------------------------------------------------------- tokens

export type TokenSet = { accessToken: string; refreshToken?: string; expiresAt?: Date; scope?: string }

export type TokenEndpoint = { tokenEndpoint: string; resource: string } & Pick<Client, 'clientId' | 'clientSecret' | 'authMethod'>

/**
 * One token request (RFC 6749 §4.1.3 or §6) with `resource` (RFC 8707 §2). An error response
 * is TokenRefusedError (sign in again) unless its code is a transient one; any other failure
 * is an OAuthError the caller treats as "try again later". GitHub answers errors with HTTP
 * 200 and an `error` member (its docs, "Error codes for the OAuth app authorization flow"),
 * so an `error` member decides, whatever the status.
 */
export async function tokenRequest(
  endpoint: TokenEndpoint,
  params: Record<string, string>,
  now: () => Date = () => new Date(),
  resolve: Resolver = systemResolver,
): Promise<TokenSet> {
  const form = new URLSearchParams({ ...params, resource: endpoint.resource })
  const headers: Record<string, string> = {
    'content-type': 'application/x-www-form-urlencoded',
    accept: 'application/json',
  }
  if (endpoint.authMethod === 'client_secret_basic' && endpoint.clientSecret) {
    const enc = (v: string) => encodeURIComponent(v)
    headers.authorization = `Basic ${Buffer.from(`${enc(endpoint.clientId)}:${enc(endpoint.clientSecret)}`).toString('base64')}`
  } else {
    form.set('client_id', endpoint.clientId)
    if (endpoint.authMethod === 'client_secret_post' && endpoint.clientSecret) form.set('client_secret', endpoint.clientSecret)
  }
  const started = now()
  const got = await fetchChecked(endpoint.tokenEndpoint, 'server', resolve, 'token_endpoint', {
    method: 'POST',
    headers,
    body: form.toString(),
  })
  const doc = parseJson(got.body)
  if (isRecord(doc) && typeof doc.error === 'string') {
    const code = safeText(doc.error) ?? 'error'
    if (TRANSIENT_ERRORS.has(code)) throw new OAuthError(`the token endpoint is unavailable (${code}); try again`)
    throw new TokenRefusedError(code, safeText(doc.error_description))
  }
  if (got.status !== 200 || !isRecord(doc)) {
    throw new OAuthError(`the token endpoint answered HTTP ${got.status}${isRecord(doc) ? '' : ' without JSON'}; try again`)
  }
  if (typeof doc.access_token !== 'string' || !doc.access_token) {
    throw new OAuthError('the token endpoint gave no access_token')
  }
  if (typeof doc.token_type !== 'string' || doc.token_type.toLowerCase() !== 'bearer') {
    throw new OAuthError(`the token endpoint gave token_type ${JSON.stringify(doc.token_type)}, not Bearer`)
  }
  const out: TokenSet = { accessToken: doc.access_token }
  if (typeof doc.refresh_token === 'string' && doc.refresh_token) out.refreshToken = doc.refresh_token
  const expires = typeof doc.expires_in === 'number' ? doc.expires_in : typeof doc.expires_in === 'string' ? Number(doc.expires_in) : NaN
  if (Number.isFinite(expires) && expires > 0) out.expiresAt = new Date(started.getTime() + expires * 1000)
  if (typeof doc.scope === 'string') out.scope = doc.scope
  return out
}
