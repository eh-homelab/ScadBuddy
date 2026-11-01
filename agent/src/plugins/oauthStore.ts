import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { type Resolver, systemResolver } from '../http/egress.js'
import { type Envelope, type Kek, openSecret, rewrap, SealError, sealSecret } from '../secrets.js'
import { notifyWarmEvict } from '../sessions/warmEvict.js'
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
// has more than EARLY_MS left, else from the row, else by refreshing under the row's lock
// (`FOR UPDATE`), so two replicas never spend one refresh token twice (OAuth 2.1 §4.3.1:
// a public client's refresh token rotates). A refusal (an RFC 6749 §5.2 error) marks the
// grant `error` with a reason the Settings row shows, "Sign in again", and the plugin is
// left out of turns until then (registry.ts `enabled`). An unreachable token endpoint
// marks nothing; the call that needed it fails.
//
// SECRETS. One envelope per row (src/secrets.ts): {access_token, refresh_token?,
// client_secret?} for a grant, {state, verifier, client_secret?} for a flow. The AAD binds
// it to the row and to everywhere its contents are sent (the plugin's URL, the resource,
// the issuer, the token endpoint, the client id), so a row edited in the database fails
// to open instead of sending a token somewhere new. Nothing here logs a value.

export const FLOW_TTL_MS = 10 * 60_000
/** An access token with less than this left is refreshed before use. */
export const EARLY_MS = 60_000
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
  plugin_url: string
  resource: string
  issuer: string
  token_endpoint: string
  client_id: string
  auth_method: AuthMethod
  registration: 'dynamic' | 'configured'
  scope: string | null
  secret_sealed: Buffer
  dek_sealed: Buffer
  kek_id: string
  has_refresh: boolean
  access_expires_at: Date | null
  status: 'connected' | 'error'
  last_error: string | null
  grant_id: string
}

type GrantSecret = { access_token: string; refresh_token?: string; client_secret?: string }
type FlowSecret = { state: string; verifier: string; client_secret?: string }

export function grantAad(r: Pick<GrantRow, 'plugin' | 'plugin_url' | 'resource' | 'issuer' | 'token_endpoint' | 'client_id'>): string {
  return `ai_plugin_oauth:${JSON.stringify({
    plugin: r.plugin,
    plugin_url: r.plugin_url,
    resource: r.resource,
    issuer: r.issuer,
    token_endpoint: r.token_endpoint,
    client_id: r.client_id,
  })}`
}

function flowAad(stateHash: string, plugin: string, pluginUrl: string, meta: FlowMeta): string {
  return `ai_plugin_oauth_flows:${JSON.stringify({
    state_hash: stateHash,
    plugin,
    plugin_url: pluginUrl,
    issuer: meta.issuer,
    token_endpoint: meta.token_endpoint,
    client_id: meta.client_id,
    redirect_uri: meta.redirect_uri,
  })}`
}

function envelope(row: { secret_sealed: Buffer; dek_sealed: Buffer; kek_id: string }): Envelope {
  return { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id }
}

function needKek(kek: Kek | undefined): Kek {
  if (!kek) {
    throw new OAuthError(
      'a plugin cannot be signed in: no key-encryption key is configured (SCADBUDDY_SECRET_KEY_FILE, spec §9)',
      409,
    )
  }
  return kek
}

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
  private readonly sql: Sql
  private readonly resolve: Resolver
  private readonly now: () => Date
  /** plugin → this process's last access token, its grant, and when the row was last read. */
  private readonly cache = new Map<string, { grant: string; value: string; expiresAt: number | null; readAt: number }>()
  /** plugin and grant → the read or refresh under way, so a burst of requests waits for one. */
  private readonly inflight = new Map<string, Promise<Header>>()

  constructor(sql: Sql, options: PluginOAuthOptions = {}) {
    this.sql = sql
    this.resolve = options.resolve ?? systemResolver
    this.now = options.now ?? (() => new Date())
  }

  // ------------------------------------------------------------------ the flow

  async start(input: StartInput, kek: Kek | undefined): Promise<Started> {
    const key = needKek(kek)
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
    const sealed = sealSecret(key, JSON.stringify(secret), flowAad(stateHash, input.plugin.name, input.plugin.url, meta))
    const expires = new Date(this.now().getTime() + FLOW_TTL_MS)
    await this.sql.begin(async (tx) => {
      await tx`DELETE FROM ai_plugin_oauth_flows WHERE expires_at < ${this.now()}`
      await tx`
        INSERT INTO ai_plugin_oauth_flows
          (state_hash, plugin, plugin_url, ticket_hash, meta, secret_sealed, dek_sealed, kek_id, created_by, expires_at)
        VALUES (${stateHash}, ${input.plugin.name}, ${input.plugin.url}, ${sha256(ticket)}, ${tx.json(meta as never)},
                ${sealed.secretSealed}, ${sealed.dekSealed}, ${sealed.kekId}, ${input.by}, ${expires})`
    })
    return { ticket, issuer: meta.issuer, registration: client.registration, scope, redirect_uri: input.redirectUri }
  }

  /**
   * Spends `ticket` once: binds its flow to `binding` (the cookie the route sets) and gives
   * the authorization URL to redirect to. Undefined for an unknown, spent or expired ticket.
   */
  async authorize(ticket: string, binding: string, kek: Kek | undefined): Promise<string | undefined> {
    if (!kek) return undefined
    const [row] = await this.sql<{ state_hash: string; plugin: string; plugin_url: string; meta: FlowMeta; secret_sealed: Buffer; dek_sealed: Buffer; kek_id: string }[]>`
      UPDATE ai_plugin_oauth_flows SET ticket_hash = NULL, binding_hash = ${sha256(binding)}
      WHERE ticket_hash = ${sha256(ticket)} AND binding_hash IS NULL AND expires_at > ${this.now()}
      RETURNING state_hash, plugin, plugin_url, meta, secret_sealed, dek_sealed, kek_id`
    if (!row) return undefined
    const secret = JSON.parse(openSecret(kek, envelope(row), flowAad(row.state_hash, row.plugin, row.plugin_url, row.meta))) as FlowSecret
    return authorizationUrl({
      server: { authorization_endpoint: row.meta.authorization_endpoint },
      clientId: row.meta.client_id,
      redirectUri: row.meta.redirect_uri,
      state: secret.state,
      verifier: secret.verifier,
      resource: row.meta.resource,
      scope: row.meta.scope ?? undefined,
    })
  }

  /** The callback. Returns the plugin signed in; every refusal is an OAuthError the page shows. */
  async complete(input: CallbackInput, kek: Kek | undefined): Promise<{ plugin: string }> {
    if (!input.state) throw new OAuthError('the sign-in answer has no state', 400)
    const stateHash = sha256(input.state)
    // Deleted first, by its state and the browser's binding: a state is good once, whatever
    // happens next, and a request without the cookie cannot spend someone else's sign-in.
    const binding = input.binding ? sha256(input.binding) : ''
    const [row] = await this.sql<{ plugin: string; plugin_url: string; meta: FlowMeta; secret_sealed: Buffer; dek_sealed: Buffer; kek_id: string; created_by: string; expires_at: Date }[]>`
      DELETE FROM ai_plugin_oauth_flows WHERE state_hash = ${stateHash} AND binding_hash = ${binding}
      RETURNING plugin, plugin_url, meta, secret_sealed, dek_sealed, kek_id, created_by, expires_at`
    if (!row) {
      const [held] = await this.sql`SELECT 1 FROM ai_plugin_oauth_flows WHERE state_hash = ${stateHash} AND expires_at > ${this.now()}`
      throw new OAuthError(
        held
          ? 'this sign-in was started in another browser; finish it there, or start it again from Settings in this one'
          : 'this sign-in is unknown, already used or expired; start it again from Settings',
        400,
      )
    }
    if (row.expires_at.getTime() <= this.now().getTime()) {
      throw new OAuthError('this sign-in is unknown, already used or expired; start it again from Settings', 400)
    }
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
    const key = needKek(kek)
    const secret = JSON.parse(openSecret(key, envelope(row), flowAad(stateHash, row.plugin, row.plugin_url, meta))) as FlowSecret
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
    const sealed = this.sealGrant(key, base, tokens, secret.client_secret)
    const stored = await this.sql.begin(async (tx) => {
      // Only while the plugin still stands at the URL it was signed in for.
      const [plugin] = await tx`SELECT name FROM ai_plugins WHERE name = ${row.plugin} AND url = ${row.plugin_url} FOR UPDATE`
      if (!plugin) return false
      await tx`
        INSERT INTO ai_plugin_oauth
          (plugin, plugin_url, resource, issuer, token_endpoint, client_id, auth_method, registration, scope,
           secret_sealed, dek_sealed, kek_id, has_refresh, access_expires_at, status, last_error, grant_id,
           connected_by, connected_at, refreshed_at)
        VALUES (${base.plugin}, ${base.plugin_url}, ${base.resource}, ${base.issuer}, ${base.token_endpoint},
                ${base.client_id}, ${meta.auth_method}, ${meta.registration}, ${tokens.scope ?? meta.scope},
                ${sealed.secretSealed}, ${sealed.dekSealed}, ${sealed.kekId}, ${tokens.refreshToken !== undefined},
                ${tokens.expiresAt ?? null}, 'connected', NULL, ${randomUUID()}, ${row.created_by}, ${this.now()}, NULL)
        ON CONFLICT (plugin) DO UPDATE SET
          plugin_url = EXCLUDED.plugin_url, resource = EXCLUDED.resource, issuer = EXCLUDED.issuer,
          token_endpoint = EXCLUDED.token_endpoint, client_id = EXCLUDED.client_id, auth_method = EXCLUDED.auth_method,
          registration = EXCLUDED.registration, scope = EXCLUDED.scope, secret_sealed = EXCLUDED.secret_sealed,
          dek_sealed = EXCLUDED.dek_sealed, kek_id = EXCLUDED.kek_id, has_refresh = EXCLUDED.has_refresh,
          access_expires_at = EXCLUDED.access_expires_at, status = 'connected', last_error = NULL,
          grant_id = EXCLUDED.grant_id, connected_by = EXCLUDED.connected_by, connected_at = EXCLUDED.connected_at,
          refreshed_at = NULL`
      await tx`UPDATE ai_plugins SET updated_at = now() WHERE name = ${base.plugin}`
      // A warm Claude Code was spawned with the plugins of its time (sessions/warmEvict.ts).
      await notifyWarmEvict(tx, '*')
      return true
    })
    if (!stored) throw new OAuthError(`plugin ${row.plugin} was removed or its URL changed during the sign-in; nothing was kept`, 409)
    this.cache.delete(row.plugin)
    return { plugin: row.plugin }
  }

  private sealGrant(
    kek: Kek,
    base: Parameters<typeof grantAad>[0],
    tokens: TokenSet,
    clientSecret: string | undefined,
    keepRefresh?: string,
  ): Envelope {
    const refresh = tokens.refreshToken ?? keepRefresh
    const secret: GrantSecret = {
      access_token: tokens.accessToken,
      ...(refresh ? { refresh_token: refresh } : {}),
      ...(clientSecret ? { client_secret: clientSecret } : {}),
    }
    return sealSecret(kek, JSON.stringify(secret), grantAad(base))
  }

  /** Signs the plugin out: the grant and any flow under way. True when there was a grant. */
  async forget(plugin: string, tx?: TransactionSql): Promise<boolean> {
    const run = async (t: TransactionSql) => {
      await t`DELETE FROM ai_plugin_oauth_flows WHERE plugin = ${plugin}`
      const gone = await t`DELETE FROM ai_plugin_oauth WHERE plugin = ${plugin}`
      if (gone.count > 0) await notifyWarmEvict(t, '*')
      return gone.count > 0
    }
    this.cache.delete(plugin)
    return tx ? run(tx) : this.sql.begin(run)
  }

  // ------------------------------------------------------------------ tokens

  /** The token source for a plugin signed in at `pluginUrl`, for the forwarder. */
  source(plugin: string, pluginUrl: string, grant: string, kek: Kek | undefined): TokenSource {
    return {
      grant,
      header: (refused) => this.header(plugin, pluginUrl, grant, kek, refused),
      reject: (reason) => this.mark(plugin, reason, grant),
    }
  }

  /** The `Authorization` header for `plugin` now (see TokenSource.header). Throws GrantError or OAuthError. */
  async header(plugin: string, pluginUrl: string, grant: string, kek: Kek | undefined, refused?: string): Promise<Header> {
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
    const next = this.fetchToken(plugin, pluginUrl, grant, kek, refused).finally(() => this.inflight.delete(key))
    this.inflight.set(key, next)
    return next
  }

  // The row's lock is held across the token endpoint's answer (at most OAUTH_TIMEOUT_MS), so
  // replicas never spend one refresh token twice. It takes one pool connection per plugin
  // refreshing at that moment, which one refresh per token lifetime keeps rare.
  private async fetchToken(
    plugin: string,
    pluginUrl: string,
    grant: string,
    kek: Kek | undefined,
    refused: string | undefined,
  ): Promise<Header> {
    if (!kek) throw new GrantError('no key-encryption key is configured to open the sign-in')
    type Outcome = { header: Header } | { mark: string } | { fail: Error }
    const outcome = await this.sql.begin(async (tx): Promise<Outcome> => {
      const [row] = await tx<GrantRow[]>`SELECT * FROM ai_plugin_oauth WHERE plugin = ${plugin} FOR UPDATE`
      if (!row) return { fail: new GrantError('it is not signed in') }
      if (row.status === 'error') return { fail: new GrantError(row.last_error ?? 'its sign-in failed') }
      if (row.plugin_url !== pluginUrl) return { fail: new GrantError('it was signed in at another URL') }
      if (row.grant_id !== grant) return { fail: new GrantError('it was signed in again or out since this run loaded it') }
      let secret: GrantSecret
      try {
        secret = JSON.parse(openSecret(kek, envelope(row), grantAad(row))) as GrantSecret
      } catch (err) {
        if (err instanceof SealError) return { fail: new GrantError(`its sign-in cannot be opened (${err.message})`) }
        throw err
      }
      const now = this.now().getTime()
      const fresh = row.access_expires_at === null || row.access_expires_at.getTime() - EARLY_MS > now
      // Fresh and not the one just refused (another replica may have refreshed already).
      if (fresh && bearer(secret.access_token).value !== refused) {
        this.remember(row, secret.access_token)
        return { header: bearer(secret.access_token) }
      }
      if (!secret.refresh_token) {
        return {
          mark:
            refused !== undefined
              ? 'the plugin refused its signed-in token, and the authorization server gave no refresh token: sign in again'
              : 'its access token expired, and the authorization server gave no refresh token: sign in again',
        }
      }
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
          { grant_type: 'refresh_token', refresh_token: secret.refresh_token },
          this.now,
          this.resolve,
        )
      } catch (err) {
        if (err instanceof TokenRefusedError) return { mark: `its refresh was refused (${err.code}): sign in again` }
        if (err instanceof OAuthError) return { fail: err }
        throw err
      }
      const sealed = this.sealGrant(kek, row, tokens, secret.client_secret, secret.refresh_token)
      await tx`
        UPDATE ai_plugin_oauth SET secret_sealed = ${sealed.secretSealed}, dek_sealed = ${sealed.dekSealed},
          kek_id = ${sealed.kekId}, has_refresh = ${tokens.refreshToken !== undefined || secret.refresh_token !== undefined},
          access_expires_at = ${tokens.expiresAt ?? null}, refreshed_at = ${this.now()},
          scope = coalesce(${tokens.scope ?? null}, scope)
        WHERE plugin = ${plugin}`
      this.remember({ ...row, access_expires_at: tokens.expiresAt ?? null }, tokens.accessToken)
      return { header: bearer(tokens.accessToken) }
    })
    if ('header' in outcome) return outcome.header
    if ('fail' in outcome) {
      this.cache.delete(plugin)
      throw outcome.fail
    }
    await this.mark(plugin, outcome.mark, grant)
    throw new GrantError(outcome.mark)
  }

  private remember(row: Pick<GrantRow, 'plugin' | 'grant_id' | 'access_expires_at'>, value: string): void {
    this.cache.set(row.plugin, {
      grant: row.grant_id,
      value,
      expiresAt: row.access_expires_at ? row.access_expires_at.getTime() : null,
      readAt: this.now().getTime(),
    })
  }

  /** Marks the grant failed with `reason`, shown in Settings until the person signs in again. */
  async mark(plugin: string, reason: string, grant?: string): Promise<void> {
    this.cache.delete(plugin)
    await this.sql.begin(async (tx) => {
      const updated = grant
        ? await tx`
          UPDATE ai_plugin_oauth SET status = 'error', last_error = ${reason}
          WHERE plugin = ${plugin} AND status = 'connected' AND grant_id = ${grant}`
        : await tx`
          UPDATE ai_plugin_oauth SET status = 'error', last_error = ${reason} WHERE plugin = ${plugin} AND status = 'connected'`
      if (updated.count > 0) await notifyWarmEvict(tx, '*')
    })
  }

  /** Key rotation, as PluginStore.rewrapFrom does for header secrets. Flows are short-lived and dropped. */
  async rewrapFrom(previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
    const result = { rewrapped: 0, failed: 0 }
    if (previous.id === current.id) return result
    await this.sql`DELETE FROM ai_plugin_oauth_flows WHERE kek_id = ${previous.id}`
    const rows = await this.sql<GrantRow[]>`SELECT * FROM ai_plugin_oauth WHERE kek_id = ${previous.id}`
    for (const row of rows) {
      let next: Envelope
      try {
        next = rewrap(previous, current, envelope(row), grantAad(row))
      } catch (err) {
        if (err instanceof SealError) {
          result.failed++
          continue
        }
        throw err
      }
      const updated = await this.sql`
        UPDATE ai_plugin_oauth SET dek_sealed = ${next.dekSealed}, kek_id = ${next.kekId}
        WHERE plugin = ${row.plugin} AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    }
    return result
  }
}

function bearer(token: string): Header {
  return { name: 'Authorization', value: `Bearer ${token}` }
}
