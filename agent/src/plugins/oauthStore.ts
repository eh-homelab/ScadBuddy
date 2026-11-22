import { CrudReadError } from '../api/crudRead.js'
import { CrudLeases, type CrudTarget, crudOf, type HeldLease, segment } from '../api/crudStores.js'
// Read at call time only: registry.ts imports this module too.
import { PLUGIN_NAME_RE } from './registry.js'
import { type Resolver, systemResolver } from '../http/egress.js'
import { SealError } from '../secrets.js'
import {
  type AuthMethod,
  authorizationUrl,
  checkIssuer,
  type Client,
  configuredAuthMethod,
  discover,
  OAuthError,
  randomToken,
  registerClient,
  safeText,
  sha256,
  type TokenEndpoint,
  TokenRefusedError,
  tokenRequest,
  type TokenSet,
} from './oauth.js'

// OAuth 2.1 for remote MCP plugins (#780): the rows. A sign-in is three requests, each
// its own route:
//
//   1. `start` (POST /api/v1/ai/plugins/:name/oauth/start, a UI write): discovery and the
//      client (oauth.ts), then a flow row holding the sealed `state` and PKCE verifier and
//      the hash of a one-use TICKET, valid FLOW_TTL_MS. The page opens
//      /api/v1/ai/plugin-oauth/authorize?ticket=… in a new window.
//   2. `authorize` (that GET, top-level in the person's browser): spends the ticket, binds
//      the flow to a random cookie it sets on this origin, and redirects to the
//      authorization server. A link to a ticket is good once, for one browser.
//   3. `complete` (GET /api/v1/ai/plugin-oauth/callback): the flow is the one whose state
//      hash matches, unexpired, and whose binding the request's cookie carries; it is
//      deleted before anything else (single use). `iss` is checked (RFC 9207) before the
//      code goes to the token endpoint, and the tokens are sealed into the grant row.
//
// There is one browser user (routes/approvals.ts BROWSER_USER; the UI has no login, spec
// §8.3): "bound to the person" is bound to the browser that opened the link, which is the
// browser whose UI-origin write started the flow.
//
// TOKENS. `token` gives the forwarder the access token: from this process's cache while it
// has more than EARLY_MS left, else from the row, else by refreshing under the lease
// `plugin-oauth:<plugin>`, so two replicas never spend one refresh token twice (OAuth 2.1 §4.3.1:
// a public client's refresh token rotates). A refusal (an RFC 6749 §5.2 error) marks the
// grant `error` with a reason the Settings row shows, "Sign in again", and the plugin is
// left out of turns until then (registry.ts `enabled`). An unreachable token endpoint
// marks nothing; the call that needed it fails.
//
// SECRETS. One envelope per row: {access_token, refresh_token?, client_secret?} for a
// grant, {state, verifier, client_secret?} for a flow. The rows go through the API's CRUD
// routes (`CrudOAuthRows`, backend api/crud_agent_plugins.py): the API seals and opens each
// envelope with its KEK, under an AAD that binds it to the row and to everywhere its
// contents are sent, and re-wraps them at a rotation. No KEK is needed here. Nothing here
// logs a value.
//
// A refresh holds the lease `plugin-oauth:<plugin>` (#5299 D3, `CrudLeases`) across the
// token endpoint's answer, and the refreshed tokens are kept only while it holds. The
// discovery, the token requests and the cache stay here.

export const FLOW_TTL_MS = 10 * 60_000
/** An access token with less than this left is refreshed before use. */
export const EARLY_MS = 60_000
/** How long a refresh through the routes waits for another replica's refresh lease, in seconds. */
export const REFRESH_WAIT_S = 20
/**
 * How long a process serves a cached token before it reads the row again: another process
 * (the api beside a worker, another replica) may have signed the plugin out, in again, or
 * marked it, and a token with no expiry would otherwise be served forever.
 */
export const RECHECK_MS = 60_000

export const COOKIE = 'sb_plugin_oauth'
export const OAUTH_PATH = '/api/v1/ai/plugin-oauth'
export const CALLBACK_PATH = `${OAUTH_PATH}/callback`

/** The plugin's grant cannot give a token: not signed in, refused, or past its life. Sign in again. */
export class GrantError extends Error {
  override name = 'GrantError'
}

export type Header = { name: string; value: string }

/** What a RemotePlugin carries for the forwarder (registry.ts `RemotePlugin.oauth`). */
export type TokenSource = {
  /** New at each sign-in: names the grant in fingerprints in place of its token. */
  grant: string
  /**
   * The `Authorization` header to send now. With `refused`, the value the server just answered
   * 401 to: a fresh token is fetched (or another replica's newer one used), and if none can be,
   * the grant is marked so the person signs in again. A grant signed out or replaced since the
   * source was made is refused (GrantError): the run that holds it stops using it.
   */
  header(refused?: string): Promise<Header>
  /** Marks this grant failed (the plugin refused even a renewed token): "Sign in again". */
  reject(reason: string): Promise<void>
}

type FlowMeta = {
  issuer: string
  authorization_endpoint: string
  token_endpoint: string
  iss_supported: boolean
  resource: string
  scope: string | null
  client_id: string
  auth_method: AuthMethod
  registration: 'dynamic' | 'configured'
  redirect_uri: string
}

type GrantRow = {
  plugin: string
  grant_id: string
  access_expires_at: Date | null
}

type GrantSecret = { access_token: string; refresh_token?: string; client_secret?: string }
type FlowSecret = { state: string; verifier: string; client_secret?: string }

export type StartInput = {
  plugin: { name: string; url: string }
  /** `<origin>/api/v1/ai/plugin-oauth/callback` on the origin the request came through. */
  redirectUri: string
  clientId?: string
  clientSecret?: string
  /** Replaces the discovered scope (a narrower one, say). */
  scope?: string
  by: string
}

export type Started = { ticket: string; issuer: string; registration: Client['registration']; scope: string | null; redirect_uri: string }

export type CallbackInput = {
  state: string | undefined
  code: string | undefined
  iss: string | undefined
  error: string | undefined
  errorDescription: string | undefined
  binding: string | undefined
}

export type PluginOAuthOptions = {
  resolve?: Resolver
  now?: () => Date
}

export class PluginOAuth {
  private readonly crud: CrudOAuthRows
  private readonly resolve: Resolver
  private readonly now: () => Date
  /** plugin → this process's last access token, its grant, and when the row was last read. */
  private readonly cache = new Map<string, { grant: string; value: string; expiresAt: number | null; readAt: number }>()
  /** plugin and grant → the read or refresh under way, so a burst of requests waits for one. */
  private readonly inflight = new Map<string, Promise<Header>>()

  /** `crud`: the rows, through the API's CRUD routes (area `plugins`). */
  constructor(crud: CrudOAuthRows, options: PluginOAuthOptions = {}) {
    this.crud = crud
    this.resolve = options.resolve ?? systemResolver
    this.now = options.now ?? (() => new Date())
  }

  // ------------------------------------------------------------------ the flow

  async start(input: StartInput): Promise<Started> {
    const found = await discover(input.plugin.url, this.resolve)
    let client: Client
    if (input.clientId) {
      client = {
        clientId: input.clientId,
        ...(input.clientSecret ? { clientSecret: input.clientSecret } : {}),
        authMethod: configuredAuthMethod(found.server, input.clientSecret),
        registration: 'configured',
      }
    } else {
      client = await registerClient(found.server, input.redirectUri, this.resolve)
    }
    const scope = input.scope?.trim() || found.scope || null
    const meta: FlowMeta = {
      issuer: found.server.issuer,
      authorization_endpoint: found.server.authorization_endpoint,
      token_endpoint: found.server.token_endpoint,
      iss_supported: found.server.authorization_response_iss_parameter_supported === true,
      resource: found.resource,
      scope,
      client_id: client.clientId,
      auth_method: client.authMethod,
      registration: client.registration,
      redirect_uri: input.redirectUri,
    }
    const state = randomToken()
    const verifier = randomToken()
    const ticket = randomToken()
    const stateHash = sha256(state)
    const secret: FlowSecret = { state, verifier, ...(client.clientSecret ? { client_secret: client.clientSecret } : {}) }
    const started = { ticket, issuer: meta.issuer, registration: client.registration, scope, redirect_uri: input.redirectUri }
    await this.crud.startFlow({
      state_hash: stateHash,
      plugin: input.plugin.name,
      plugin_url: input.plugin.url,
      ticket_hash: sha256(ticket),
      meta,
      secret,
      created_by: input.by,
      ttl_ms: FLOW_TTL_MS,
    })
    return started
  }

  /**
   * Spends `ticket` once: binds its flow to `binding` (the cookie the route sets) and gives
   * the authorization URL to redirect to. Undefined for an unknown, spent or expired ticket.
   */
  async authorize(ticket: string, binding: string): Promise<string | undefined> {
    const flow = await this.crud.authorize(sha256(ticket), sha256(binding))
    if (!flow) return undefined
    if (!flow.secret) throw new SealError(flow.seal_error ?? 'the sign-in cannot be opened')
    const { meta, secret } = flow
    return authorizationUrl({
      server: { authorization_endpoint: meta.authorization_endpoint },
      clientId: meta.client_id,
      redirectUri: meta.redirect_uri,
      state: secret.state,
      verifier: secret.verifier,
      resource: meta.resource,
      scope: meta.scope ?? undefined,
    })
  }

  /** The callback. Returns the plugin signed in; every refusal is an OAuthError the page shows. */
  async complete(input: CallbackInput): Promise<{ plugin: string }> {
    if (!input.state) throw new OAuthError('the sign-in answer has no state', 400)
    const stateHash = sha256(input.state)
    // Deleted first, by its state and the browser's binding: a state is good once, whatever
    // happens next, and a request without the cookie cannot spend someone else's sign-in.
    const binding = input.binding ? sha256(input.binding) : ''
    const row = await this.take(stateHash, binding)
    const meta = row.meta
    // RFC 9207 before anything else, error responses included: on a mismatch nothing of the answer is shown.
    if (!checkIssuer(meta.issuer, meta.iss_supported, input.iss)) {
      throw new OAuthError('the sign-in answer did not come from the authorization server it was sent to (iss); it was discarded', 400)
    }
    if (input.error) {
      const why = safeText(input.errorDescription)
      throw new OAuthError(`the authorization server refused the sign-in: ${safeText(input.error)}${why ? ` (${why})` : ''}`, 409)
    }
    if (!input.code) throw new OAuthError('the sign-in answer has no code', 400)
    const secret = row.open()
    if (secret.state !== input.state) throw new OAuthError('the sign-in answer does not match its flow', 400)
    const endpoint: TokenEndpoint = {
      tokenEndpoint: meta.token_endpoint,
      resource: meta.resource,
      clientId: meta.client_id,
      ...(secret.client_secret ? { clientSecret: secret.client_secret } : {}),
      authMethod: meta.auth_method,
    }
    const tokens = await tokenRequest(
      endpoint,
      { grant_type: 'authorization_code', code: input.code, redirect_uri: meta.redirect_uri, code_verifier: secret.verifier },
      this.now,
      this.resolve,
    )
    const base = {
      plugin: row.plugin,
      plugin_url: row.plugin_url,
      resource: meta.resource,
      issuer: meta.issuer,
      token_endpoint: meta.token_endpoint,
      client_id: meta.client_id,
    }
    const stored = await this.crud.storeGrant({
      ...base,
      auth_method: meta.auth_method,
      registration: meta.registration,
      scope: tokens.scope ?? meta.scope,
      secret: grantSecret(tokens, secret.client_secret),
      has_refresh: tokens.refreshToken !== undefined,
      access_expires_at: tokens.expiresAt?.toISOString() ?? null,
      connected_by: row.created_by,
    })
    if (!stored) throw new OAuthError(`plugin ${row.plugin} was removed or its URL changed during the sign-in; nothing was kept`, 409)
    this.cache.delete(row.plugin)
    return { plugin: row.plugin }
  }

  /** The callback's flow: the API deletes it first, by its state and the browser's binding, and opens it. */
  private async take(
    stateHash: string,
    binding: string,
  ): Promise<{ plugin: string; plugin_url: string; meta: FlowMeta; created_by: string; open: () => FlowSecret }> {
    const { flow, held } = await this.crud.take(stateHash, binding)
    if (!flow) throw notTaken(held)
    if (flow.expired) throw notTaken(false)
    return {
      ...flow,
      open: () => {
        if (!flow.secret) throw new OAuthError(`this sign-in cannot be opened on the API (${flow.seal_error ?? 'no secret'}); start it again`, 409)
        return flow.secret
      },
    }
  }

  /** Signs the plugin out: the grant and any flow under way. True when there was a grant. */
  async forget(plugin: string): Promise<boolean> {
    this.cache.delete(plugin)
    return this.crud.forget(plugin)
  }

  // ------------------------------------------------------------------ tokens

  /** The token source for a plugin signed in at `pluginUrl`, for the forwarder. */
  source(plugin: string, pluginUrl: string, grant: string): TokenSource {
    return {
      grant,
      header: (refused) => this.header(plugin, pluginUrl, grant, refused),
      reject: (reason) => this.mark(plugin, reason, grant),
    }
  }

  /** The `Authorization` header for `plugin` now (see TokenSource.header). Throws GrantError or OAuthError. */
  async header(plugin: string, pluginUrl: string, grant: string, refused?: string): Promise<Header> {
    const cached = this.cache.get(plugin)
    const now = this.now().getTime()
    if (
      refused === undefined &&
      cached?.grant === grant &&
      now - cached.readAt < RECHECK_MS &&
      (cached.expiresAt === null || cached.expiresAt - EARLY_MS > now)
    ) {
      return bearer(cached.value)
    }
    const key = `${plugin}\n${grant}`
    const running = this.inflight.get(key)
    if (running) {
      const got = await running
      if (refused === undefined || got.value !== refused) return got
    }
    const next = this.fetchToken(plugin, pluginUrl, grant, refused).finally(() => this.inflight.delete(key))
    this.inflight.set(key, next)
    return next
  }

  private async fetchToken(plugin: string, pluginUrl: string, grant: string, refused: string | undefined): Promise<Header> {
    const outcome = await this.fetchThrough(plugin, pluginUrl, grant, refused)
    if ('header' in outcome) return outcome.header
    if ('fail' in outcome) {
      this.cache.delete(plugin)
      throw outcome.fail
    }
    await this.mark(plugin, outcome.mark, grant)
    throw new GrantError(outcome.mark)
  }

  /**
   * The grant opened by the API; a refresh under the lease `plugin-oauth:<plugin>` (held
   * across the token endpoint's answer, so replicas never spend one refresh token twice), the
   * grant read again inside it (another replica may have refreshed while this one waited),
   * and the new tokens kept only while the lease holds.
   */
  private async fetchThrough(plugin: string, pluginUrl: string, grant: string, refused: string | undefined): Promise<Outcome> {
    const crud = this.crud
    const decide = (opened: OpenedGrantAnswer): Outcome | OpenedGrant => {
      if (!opened.grant) return { fail: new GrantError(opened.fail ?? 'it is not signed in') }
      const row = opened.grant
      const expiresAt = row.access_expires_at === null ? null : new Date(row.access_expires_at)
      const fresh = expiresAt === null || expiresAt.getTime() - EARLY_MS > this.now().getTime()
      if (fresh && bearer(row.secret.access_token).value !== refused) {
        this.remember({ plugin, grant_id: row.grant_id, access_expires_at: expiresAt }, row.secret.access_token)
        return { header: bearer(row.secret.access_token) }
      }
      if (!row.secret.refresh_token) return { mark: noRefreshReason(refused) }
      return row
    }
    const first = decide(await crud.openGrant(plugin, pluginUrl, grant))
    if (!('secret' in first)) return first
    const held = await crud.leases.holding(
      refreshLease(plugin),
      async (lease): Promise<Outcome> => {
        const row = decide(await crud.openGrant(plugin, pluginUrl, grant))
        if (!('secret' in row)) return row
        const { secret } = row
        let tokens: TokenSet
        try {
          tokens = await tokenRequest(
            {
              tokenEndpoint: row.token_endpoint,
              resource: row.resource,
              clientId: row.client_id,
              ...(secret.client_secret ? { clientSecret: secret.client_secret } : {}),
              authMethod: row.auth_method,
            },
            { grant_type: 'refresh_token', refresh_token: secret.refresh_token! },
            this.now,
            this.resolve,
          )
        } catch (err) {
          if (err instanceof TokenRefusedError) return { mark: `its refresh was refused (${err.code}): sign in again` }
          if (err instanceof OAuthError) return { fail: err }
          throw err
        }
        const kept = await crud.refreshed(plugin, lease, {
          grant,
          secret: grantSecret(tokens, secret.client_secret, secret.refresh_token),
          has_refresh: tokens.refreshToken !== undefined || secret.refresh_token !== undefined,
          access_expires_at: tokens.expiresAt?.toISOString() ?? null,
          scope: tokens.scope ?? null,
        })
        if (kept === 'lease-lost') {
          // A provider that rotates refresh tokens has spent the stored one: the next refresh is
          // refused and marks the grant, so "try again" would not help.
          const rotated = tokens.refreshToken !== undefined && tokens.refreshToken !== secret.refresh_token
          return { fail: new OAuthError(`its renewed token was not kept: the refresh took longer than its lease; ${rotated ? 'a new sign-in may be needed' : 'try again'}`, 409) }
        }
        // The API keeps a refresh only on the same grant, still connected (crud_agent_plugins.py).
        if (!kept) return { fail: new GrantError('it was signed in again, out, or marked for a new sign-in since this run loaded it') }
        this.remember({ plugin, grant_id: grant, access_expires_at: tokens.expiresAt ?? null }, tokens.accessToken)
        return { header: bearer(tokens.accessToken) }
      },
      { waitS: REFRESH_WAIT_S },
    )
    if (held === 'busy') return { fail: new OAuthError('another replica is renewing its access token; try again shortly', 409) }
    return held.value
  }

  private remember(row: Pick<GrantRow, 'plugin' | 'grant_id' | 'access_expires_at'>, value: string): void {
    this.cache.set(row.plugin, {
      grant: row.grant_id,
      value,
      expiresAt: row.access_expires_at ? row.access_expires_at.getTime() : null,
      readAt: this.now().getTime(),
    })
  }

  /**
   * Every secret a stored sign-in holds (its access and refresh tokens and its client
   * secret), opened by the API, for a search that must not let one leave the agent
   * (plugins/secrets.ts `readPluginSecrets`). Read from the rows alone: nothing is
   * refreshed and no endpoint is contacted. A row the API's key does not open gives nothing.
   */
  async storedSecrets(): Promise<string[]> {
    const secrets = await this.crud.storedSecrets()
    const out: string[] = []
    for (const secret of secrets) {
      out.push(secret.access_token, bearer(secret.access_token).value)
      if (secret.refresh_token) out.push(secret.refresh_token)
      if (secret.client_secret) out.push(secret.client_secret)
    }
    return out
  }

  /** Marks the grant failed with `reason`, shown in Settings until the person signs in again. */
  async mark(plugin: string, reason: string, grant?: string): Promise<void> {
    this.cache.delete(plugin)
    await this.crud.mark(plugin, reason, grant)
  }
}

function bearer(token: string): Header {
  return { name: 'Authorization', value: `Bearer ${token}` }
}

type Outcome = { header: Header } | { mark: string } | { fail: Error }

function noRefreshReason(refused: string | undefined): string {
  return refused !== undefined
    ? 'the plugin refused its signed-in token, and the authorization server gave no refresh token: sign in again'
    : 'its access token expired, and the authorization server gave no refresh token: sign in again'
}

function notTaken(held: boolean): OAuthError {
  return new OAuthError(
    held
      ? 'this sign-in was started in another browser; finish it there, or start it again from Settings in this one'
      : 'this sign-in is unknown, already used or expired; start it again from Settings',
    400,
  )
}

/** What a grant's envelope holds, in the key order both sides seal it. */
function grantSecret(tokens: TokenSet, clientSecret: string | undefined, keepRefresh?: string): GrantSecret {
  const refresh = tokens.refreshToken ?? keepRefresh
  return {
    access_token: tokens.accessToken,
    ...(refresh ? { refresh_token: refresh } : {}),
    ...(clientSecret ? { client_secret: clientSecret } : {}),
  }
}

/** The lease a refresh holds (backend agent_data/leases.py). */
export const refreshLease = (plugin: string): string => `plugin-oauth:${plugin}`

// ------------------------------------------------------------- the CRUD routes

/** A grant's secret as the API answers it: a field it does not hold is null. */
type WireSecret = { access_token: string; refresh_token?: string | null; client_secret?: string | null }

function secretOf(wire: WireSecret): GrantSecret {
  return {
    access_token: wire.access_token,
    ...(wire.refresh_token ? { refresh_token: wire.refresh_token } : {}),
    ...(wire.client_secret ? { client_secret: wire.client_secret } : {}),
  }
}

type FlowSecretWire = { state: string; verifier: string; client_secret?: string | null }

function flowSecretOf(wire: FlowSecretWire): FlowSecret {
  return { state: wire.state, verifier: wire.verifier, ...(wire.client_secret ? { client_secret: wire.client_secret } : {}) }
}

type FlowWire = { plugin: string; plugin_url: string; meta: FlowMeta; secret: FlowSecretWire | null; seal_error: string | null }
export type OpenedFlow = Omit<FlowWire, 'secret'> & { secret: FlowSecret | null }
export type TakenFlow = OpenedFlow & { created_by: string; expired: boolean }

export type OpenedGrant = {
  plugin: string
  plugin_url: string
  resource: string
  issuer: string
  token_endpoint: string
  client_id: string
  auth_method: AuthMethod
  grant_id: string
  access_expires_at: string | null
  secret: GrantSecret
}
export type OpenedGrantAnswer = { grant: OpenedGrant | null; fail: string | null }

export type FlowStart = {
  state_hash: string
  plugin: string
  plugin_url: string
  ticket_hash: string
  meta: FlowMeta
  secret: FlowSecret
  created_by: string
  ttl_ms: number
}

export type GrantStore = {
  plugin: string
  plugin_url: string
  resource: string
  issuer: string
  token_endpoint: string
  client_id: string
  auth_method: AuthMethod
  registration: 'dynamic' | 'configured'
  scope: string | null
  secret: GrantSecret
  has_refresh: boolean
  access_expires_at: string | null
  connected_by: string
}

export type GrantRefreshed = {
  grant: string
  secret: GrantSecret
  has_refresh: boolean
  access_expires_at: string | null
  scope: string | null
}

const KEK_MISSING = 'kek-missing'
const PLUGIN_GONE = 'plugin-gone'
const LEASE_LOST = 'lease-lost'

/**
 * The sign-ins' rows through the API's CRUD routes, area `plugins` (backend
 * api/crud_agent_plugins.py): the API seals each envelope with its KEK and opens it only for the call that needs it (a redirect, a callback, a token
 * request, the redaction list). `leases` holds a refresh's lease.
 */
export class CrudOAuthRows {
  readonly #call: ReturnType<typeof crudOf>
  readonly leases: CrudLeases

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
    this.leases = new CrudLeases(target)
  }

  async startFlow(body: FlowStart): Promise<void> {
    try {
      await this.#call('a plugin sign-in started', '/plugin-oauth/flows', { body })
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === PLUGIN_GONE) throw new OAuthError(err.detail, 409)
      if (err instanceof CrudReadError && err.fromRoute && err.reason === KEK_MISSING) {
        throw new OAuthError('a plugin cannot be signed in: no key-encryption key is configured on the API (SCADBUDDY_SECRET_KEY_FILE, spec §9)', 409)
      }
      throw err
    }
  }

  /** The flow its ticket names, bound to `bindingHash`; undefined for an unknown, spent or expired ticket, or no KEK on the API. */
  async authorize(ticketHash: string, bindingHash: string): Promise<OpenedFlow | undefined> {
    try {
      const { flow } = await this.#call<{ flow: FlowWire | null }>('a plugin sign-in opened', '/plugin-oauth/flows/authorize', {
        body: { ticket_hash: ticketHash, binding_hash: bindingHash },
      })
      return flow ? { ...flow, secret: flow.secret ? flowSecretOf(flow.secret) : null } : undefined
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === KEK_MISSING) return undefined
      throw err
    }
  }

  async take(stateHash: string, bindingHash: string): Promise<{ flow: TakenFlow | null; held: boolean }> {
    const answer = await this.#call<{ flow: (FlowWire & { created_by: string; expired: boolean }) | null; held: boolean }>(
      'a plugin sign-in completed',
      '/plugin-oauth/flows/take',
      { body: { state_hash: stateHash, binding_hash: bindingHash } },
    )
    const { flow } = answer
    return { held: answer.held, flow: flow ? { ...flow, secret: flow.secret ? flowSecretOf(flow.secret) : null } : null }
  }

  /** Whether the grant was kept: false when the plugin was removed or moved since. */
  async storeGrant(body: GrantStore): Promise<boolean> {
    try {
      return (await this.#call<{ stored: boolean }>('a plugin sign-in kept', '/plugin-oauth/grants', { body })).stored
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === KEK_MISSING) {
        throw new OAuthError('a plugin cannot be signed in: no key-encryption key is configured on the API (SCADBUDDY_SECRET_KEY_FILE, spec §9)', 409)
      }
      throw err
    }
  }

  async openGrant(plugin: string, pluginUrl: string, grant: string): Promise<OpenedGrantAnswer> {
    const answer = await this.#call<{ grant: (Omit<OpenedGrant, 'secret'> & { secret: WireSecret }) | null; fail: string | null }>(
      "a plugin sign-in's token",
      `/plugin-oauth/grants/${segment(plugin, PLUGIN_NAME_RE, 'a plugin name')}/open`,
      { body: { plugin_url: pluginUrl, grant } },
    )
    return { fail: answer.fail, grant: answer.grant ? { ...answer.grant, secret: secretOf(answer.grant.secret) } : null }
  }

  /** Whether the refreshed grant was kept; `lease-lost` when `lease` no longer held. */
  async refreshed(plugin: string, lease: HeldLease, body: GrantRefreshed): Promise<boolean | 'lease-lost'> {
    try {
      return (
        await this.#call<{ stored: boolean }>("a plugin sign-in's renewal", `/plugin-oauth/grants/${segment(plugin, PLUGIN_NAME_RE, 'a plugin name')}/refreshed`, {
          body: { lease_token: lease.token, ...body },
        })
      ).stored
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === LEASE_LOST) return LEASE_LOST
      throw err
    }
  }

  async mark(plugin: string, reason: string, grant?: string): Promise<void> {
    await this.#call('a plugin sign-in marked', `/plugin-oauth/grants/${segment(plugin, PLUGIN_NAME_RE, 'a plugin name')}/mark`, {
      body: { reason: reason.slice(0, 2048), ...(grant ? { grant } : {}) },
    })
  }

  async forget(plugin: string): Promise<boolean> {
    return (await this.#call<{ done: boolean }>('a plugin signed out', `/plugin-oauth/grants/${segment(plugin, PLUGIN_NAME_RE, 'a plugin name')}/forget`, { method: 'POST' })).done
  }

  async storedSecrets(): Promise<GrantSecret[]> {
    const { secrets } = await this.#call<{ secrets: WireSecret[] }>('the plugin sign-ins', '/plugin-oauth/stored-secrets', { method: 'POST' })
    return secrets.map(secretOf)
  }
}
