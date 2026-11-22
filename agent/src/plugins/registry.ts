import type { AuditContext } from '../audit/log.js'
import { type AccessRow, remoteSource, type ToolAccess } from '../access/model.js'
import { pluginOverrides, sourceEnabled } from '../access/resolve.js'
import { updatedBy } from '../access/store.js'
import { isLoopbackPeer } from '../http/origins.js'
import { assertHostAllowed, EgressError, isPrivateAddress, type Resolver, systemResolver } from '../http/egress.js'
import { RISK_TIERS, type RiskTier, type TierResolver } from '../harness/permissions.js'
import { CrudReadError } from '../api/crudRead.js'
import { type CrudTarget, crudOf, segment } from '../api/crudStores.js'
import { SealError } from '../secrets.js'
import { OAuthError } from './oauth.js'
import { GrantError, type PluginOAuth, type TokenSource } from './oauthStore.js'

// The plugin registry (issue #297): "provide an endpoint and we'll add the
// plugin/skills to the harness". Kept by the API, in `ai_plugins`
// (backend/scadbuddy/migrations_agent/<timestamp>_plugins.sql; spec §9), through its CRUD routes.
// No file is written.
//
// WHAT A PLUGIN IS HERE. A remote MCP server: a Streamable HTTP URL plus an
// optional auth header. The harness registers it as an Agent SDK
// `McpHttpServerConfig` (`{ type: 'http', url }`, sdk.d.ts 0.3.283 and 0.3.287) under the
// plugin's name, so its tools reach the model as `mcp__<name>__<tool>` ("MCP
// tools follow the naming pattern mcp__{server_name}__{tool_name}",
// https://code.claude.com/docs/en/agent-sdk/mcp). The URL Claude Code gets is
// NOT the plugin's: it is a loopback forwarder in this process
// (forwarder.ts), which connects to the checked address, refuses redirects
// and OAuth discovery, and adds the header itself, so Claude Code never holds
// the secret.
//
// TOOL NAMES. Claude Code rewrites every character outside [A-Za-z0-9_-] in
// a tool name to `_` (`harnessToolName`; spec §3.1), so `files.list` and
// `files_list` would share one name. `tool_tiers` keys must therefore already
// be in that alphabet (a tool named otherwise stays `outward`), and the
// forwarder hides colliding tools and refuses a call whose raw name differs
// from the tiered one.
// Plugin PACKAGES (skills, subagents, hooks from a git URL at a pinned commit,
// issue #297 "Installing a plugin") are a separate table and module:
// src/plugins/packages/ (`ai_plugin_packages`), where Postgres holds the pin
// and the files on disk are only a verified cache.
//
// TRANSPORT. Spec D5: "MCP: Streamable HTTP only, over HTTPS". So `type: 'http'`
// only (the SDK's `'sse'` is the legacy HTTP+SSE transport D5 rejects), and the
// URL is https unless every address it resolves to is loopback or private
// (10/8, 172.16/12, 192.168/16, fc00::/7): an MCP server in the same cluster,
// such as the Playwright MCP Service, or on the LAN, which never leaves the
// private network. A public host still needs https. The host must also
// pass the egress check (http/egress.ts: no link-local, no cloud metadata),
// at save, at test, and each time a harness run loads the plugin.
//
// TIERS (spec §8.1: "A plugin tool ScadBuddy doesn't recognise defaults to
// outward"). Every tool of a plugin is `outward`, so it needs approval, until an
// admin sets it explicitly in `tool_tiers`. MCP annotations are never used to
// decide: the MCP spec says "clients MUST consider tool annotations to be
// untrusted unless they come from trusted servers"
// (https://modelcontextprotocol.io/specification/2025-06-18/server/tools); the
// connection test only SHOWS `readOnlyHint` as a suggestion.
//
// SECRETS. The header value is sealed with the envelope in src/secrets.ts. Its
// AAD binds it to the plugin's name, URL and header name, so a row whose URL
// was edited in the database fails to open instead of sending the token to a
// new host; for the same reason a route that changes the URL or the header
// name must be given the secret again. No route returns it (spec §8.6). The API
// seals and opens the value with its KEK (backend api/crud_agent_plugins.py), so no
// KEK is needed here. The API re-wraps both at a rotation.
//
// OAUTH (#780). Instead of a header, a plugin may be signed in from Settings (OAuth 2.1 per
// the MCP authorization spec; plugins/oauth.ts, oauthStore.ts, table `ai_plugin_oauth`).
// Its tokens never leave the agent either: a run gets `RemotePlugin.oauth`, the token
// source the forwarder asks for a fresh `Authorization` value per request, and `header`
// holds the value at load (for the redaction lists and the memory client). A plugin has a
// header or a grant, never both: saving a header signs it out, and a URL change drops the
// grant (it was issued for the old resource).

export const PLUGIN_KINDS = ['remote_mcp'] as const
export type PluginKind = (typeof PLUGIN_KINDS)[number]

/** 2–32 characters: lower-case letters, digits, single hyphens; starts with a letter. */
export const PLUGIN_NAME_RE = /^[a-z][a-z0-9-]{0,30}[a-z0-9]$/

/**
 * Names a plugin may not take: the in-process ScadBuddy server (#251's
 * `mcp__scadbuddy__*`), whose tools have tiers of their own, plus the vendor names, plus the server names Claude Code
 * treats specially and drops or never offers tools from. Read from the pinned
 * CLI (2.1.289, the native binary of @anthropic-ai/claude-agent-sdk-<platform>
 * 0.3.289): search the binary for the message `Cannot add MCP server "${e}":
 * this name is reserved.` and read the predicate the check before it calls,
 * and the sets that predicate tests. The minified names change every build, so
 * search for the message, never a name. That check (`claude mcp add` answers
 * "this name is reserved"; a plugin's server is dropped) covers `workspace`,
 * `computer-use`, `claude-in-chrome`, `claude-device`, `hearthbot`,
 * `remote-devices`, `webagent` and `claude-code-remote` in a local run, and
 * `widgets` only in a hosted or CLAUDE_CODE_REMOTE run. `ide` is the IDE
 * connection's server. Its other reserved names cannot pass PLUGIN_NAME_RE.
 */
export const RESERVED_PLUGIN_NAMES: ReadonlySet<string> = new Set([
  'scadbuddy',
  'claude',
  'anthropic',
  'plugin',
  'workspace',
  'computer-use',
  'claude-in-chrome',
  'claude-device',
  'hearthbot',
  'remote-devices',
  'webagent',
  'claude-code-remote',
  'ide',
])

/**
 * A tool name as Claude Code puts it in `mcp__<server>__<tool>`: every
 * character outside [A-Za-z0-9_-] becomes `_` (CLI 2.1.283 and 2.1.287; spec §3.1).
 */
export function harnessToolName(tool: string): string {
  return tool.replace(/[^a-zA-Z0-9_-]/g, '_')
}

/**
 * `tool_tiers` keys: names Claude Code does not rewrite, so the tier the
 * permission seam sees (by harness name) is the tier of exactly that tool.
 */
export const TIERED_TOOL_NAME_RE = /^[A-Za-z0-9_-]{1,128}$/
/** `disabled_tools` entries: any MCP tool name up to 128 characters, no control characters. */
// eslint-disable-next-line no-control-regex
const DISABLED_TOOL_NAME_RE = /^[^\u0000-\u001f\u007f]{1,128}$/
export const MAX_TOOL_ENTRIES = 256

/** An HTTP token (RFC 9110 §5.6.2) of at most 64 characters. */
const HEADER_NAME_RE = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/
/** Headers the MCP transport sets itself, or that would change what the request is. */
const RESERVED_HEADERS: ReadonlySet<string> = new Set([
  'host',
  'content-type',
  'content-length',
  'accept',
  'connection',
  'transfer-encoding',
  'cookie',
  'mcp-session-id',
  'mcp-protocol-version',
  'last-event-id',
  'proxy-authorization',
  'te',
  'upgrade',
  'expect',
  'keep-alive',
  'trailer',
  'user-agent',
  'origin',
  'accept-encoding',
])

/**
 * What to redact for a header value: the value, and the token after an
 * auth-scheme prefix (`Bearer`, `Basic`, `Token`), which a server or a log
 * line may echo on its own.
 */
export function headerSecretVariants(value: string): string[] {
  const scheme = /^(?:bearer|basic|token)\s+(\S.*)$/i.exec(value)
  return scheme?.[1] ? [value, scheme[1]] : [value]
}

export const DEFAULT_AUTH_HEADER = 'Authorization'

/**
 * The remote plugin whose endpoint and bank the assistant's automatic memory
 * uses (memory/hindsight.ts): a Hindsight MCP URL, `…/mcp/<bank_id>/`.
 */
export const HINDSIGHT_PLUGIN = 'hindsight'

/** A request the registry refuses; `status` is the HTTP status the route answers with. */
export class PluginError extends Error {
  override name = 'PluginError'
  readonly status: 400 | 404 | 409 | 422 | 502 | 503
  constructor(message: string, status: 400 | 404 | 409 | 422 | 502 | 503) {
    super(message)
    this.status = status
  }
}

/** A stored plugin, as routes may see it (no secret). */
export type PluginSummary = {
  name: string
  kind: PluginKind
  url: string
  enabled: boolean
  auth_header: string | null
  /** Last four characters of the header value; '' for a short one; null when there is none. */
  secret_last4: string | null
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
  created_at: string
  updated_at: string
  /** Whether the API's key opens the secret; true when there is none. */
  opens: boolean
  /** The OAuth sign-in (#780), or null when the plugin has none. */
  oauth: OAuthStatus | null
}

/** A plugin's OAuth sign-in as routes may see it: no token, no client secret. */
export type OAuthStatus = {
  status: 'connected' | 'error'
  /** Why it must be signed in again (`error`), or null. */
  error: string | null
  issuer: string
  scope: string | null
  registration: 'dynamic' | 'configured'
  /** Whether the agent holds a refresh token, so it renews the access token itself. */
  refreshes: boolean
  expires_at: string | null
  connected_at: string
}

/**
 * The routes never pass `enabled`, `tool_tiers` or `disabled_tools` (#4828: they are
 * `ai_access` rows, refused by the routes). The one writer in `src/` is
 * `access/providers.ts` `setEnabled`, which patches `enabled` and nothing else; the tier
 * fields are for tests that seed a plugin's access in one call.
 */
export type PluginCreate = {
  name: string
  url: string
  enabled?: boolean
  auth_header?: string | null
  secret?: string
  tool_tiers?: Record<string, RiskTier>
  disabled_tools?: string[]
}

/**
 * Omitted fields are kept. `secret: null` removes the auth header and its value. The access
 * fields (`enabled`, `tool_tiers`, `disabled_tools`) are as on `PluginCreate`.
 */
export type PluginPatch = {
  url?: string
  enabled?: boolean
  auth_header?: string | null
  secret?: string | null
  tool_tiers?: Record<string, RiskTier>
  disabled_tools?: string[]
}

/** A plugin ready for the harness, secret opened. Lives for one query's set-up. */
export type RemotePlugin = {
  name: string
  url: string
  header?: { name: string; value: string }
  /** A signed-in plugin's token source (#780): the forwarder asks it for each request's header. */
  oauth?: TokenSource
  toolTiers: Record<string, RiskTier>
  disabledTools: string[]
}

/** The registry as routes and the session manager see it. */
export type PluginRepo = {
  list(): Promise<PluginSummary[]>
  get(name: string): Promise<PluginSummary | undefined>
  create(input: PluginCreate): Promise<PluginSummary>
  /** `context`: who to record on the ai_access rows the patch writes; `system` without one. */
  update(name: string, patch: PluginPatch, context?: AuditContext): Promise<PluginSummary>
  /**
   * One tool's access (#2171): `off` disables it, a tier sets it, `null` drops both.
   * Recorded as `context`'s actor.
   */
  setToolAccess(name: string, tool: string, access: ToolAccess | null, context?: AuditContext): Promise<PluginSummary>
  delete(name: string): Promise<boolean>
  /** Opens one plugin's secret. Throws SealError on a wrong key or an altered row. */
  reveal(name: string): Promise<RemotePlugin | undefined>
}

/** What `CrudPluginStore` gives: the repo, the enabled plugins and the sign-ins. */
export type PluginStoreLike = PluginRepo & {
  readonly oauth: PluginOAuth | undefined
  enabled(): Promise<{ plugins: RemotePlugin[]; problems: string[] }>
}

// ---------------------------------------------------------------- validation

export function validatePluginName(name: string): string {
  if (!PLUGIN_NAME_RE.test(name) || name.includes('--')) {
    throw new PluginError(
      'name must be 2–32 characters: lower-case letters, digits and single hyphens, starting with a letter ' +
        'and not ending with a hyphen',
      400,
    )
  }
  if (RESERVED_PLUGIN_NAMES.has(name)) throw new PluginError(`name "${name}" is reserved`, 400)
  return name
}

/**
 * Why a stored plugin cannot run under its name, or null (#2127). A row saved
 * before its name joined `RESERVED_PLUGIN_NAMES` is still in the table, but
 * Claude Code drops a server by that name without a word, so it is reported
 * rather than handed to the harness.
 */
export function reservedNameProblem(name: string): string | null {
  return RESERVED_PLUGIN_NAMES.has(name)
    ? `its name "${name}" is reserved by Claude Code, which drops the server; delete it and register it again under another name`
    : null
}

/**
 * Normalises an endpoint URL: http(s), no credentials, no query or fragment
 * (a token belongs in the sealed header, not in a URL stored in the clear), and
 * no `$` anywhere, so Claude Code's `${VAR}` expansion of MCP config values can
 * never be pointed at the credential in the query's environment. The path is
 * kept as given, trailing slash included (Hindsight's is `/mcp/{bank_id}/`).
 * Whether the scheme is allowed for the host is `assertEndpointAllowed`'s job.
 */
export function normalisePluginUrl(raw: string): string {
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    throw new PluginError('url is not a valid URL', 400)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new PluginError(`url must be https (or http to a loopback or private address), not ${url.protocol}//`, 400)
  }
  if (url.username || url.password) {
    throw new PluginError('url must not carry credentials; use auth_header and secret', 400)
  }
  if (url.search || url.hash) {
    throw new PluginError('url must not have a query or fragment; put a token in auth_header and secret', 400)
  }
  const text = url.toString()
  if (text.includes('$')) throw new PluginError('url must not contain "$"', 400)
  if (text.length > 2048) throw new PluginError('url is longer than 2048 characters', 400)
  return text
}

/**
 * Spec §8.4 and the egress rule for a URL a secret is sent to: the host must
 * not be (or resolve to) link-local or cloud metadata, and plain http is
 * allowed only when every address the host resolves to is loopback or private. Resolves
 * to the checked addresses; the forwarder connects to the first of them and
 * does not resolve the name again.
 */
export async function assertEndpointAllowed(url: string, resolve: Resolver = systemResolver): Promise<string[]> {
  const addresses = await assertHostAllowed(url, resolve, 'url', 'an MCP endpoint')
  const parsed = new URL(url)
  if (parsed.protocol === 'https:') return addresses
  if (!addresses.every((a) => isLoopbackPeer(a) || isPrivateAddress(a))) {
    throw new EgressError(
      `url must be https: plain http is allowed only to loopback or private addresses, and ${parsed.hostname} is not (spec §8.4)`,
    )
  }
  return addresses
}

export function validateHeaderName(name: string): string {
  if (!HEADER_NAME_RE.test(name)) throw new PluginError('auth_header is not a valid HTTP header name', 400)
  if (RESERVED_HEADERS.has(name.toLowerCase())) {
    throw new PluginError(`auth_header "${name}" is set by the MCP transport itself`, 400)
  }
  return name
}

export function validateSecret(raw: string): string {
  const secret = raw.trim()
  if (!secret) throw new PluginError('secret is empty', 400)
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(secret)) throw new PluginError('secret must not contain control characters', 400)
  if (secret.length > 4096) throw new PluginError('secret is longer than 4096 characters', 400)
  return secret
}

export function validateToolTiers(tiers: Record<string, string>): Record<string, RiskTier> {
  const entries = Object.entries(tiers)
  if (entries.length > MAX_TOOL_ENTRIES) throw new PluginError(`tool_tiers has more than ${MAX_TOOL_ENTRIES} entries`, 400)
  // No prototype: a tool named `__proto__` is an own key.
  const out = Object.create(null) as Record<string, RiskTier>
  for (const [tool, tier] of entries) {
    if (!TIERED_TOOL_NAME_RE.test(tool)) {
      throw new PluginError(
        `tool_tiers: "${tool}" cannot be given a tier: only tools named with A-Z a-z 0-9 _ - (1–128) can, ` +
          'because Claude Code renames any other character to "_" and two tools could then share the tier; ' +
          'such a tool stays outward (or disable it)',
        400,
      )
    }
    if (!(RISK_TIERS as readonly string[]).includes(tier)) {
      throw new PluginError(`tool_tiers.${tool} must be one of ${RISK_TIERS.join(', ')}`, 400)
    }
    out[tool] = tier as RiskTier
  }
  return out
}

export function validateDisabledTools(tools: readonly string[]): string[] {
  if (tools.length > MAX_TOOL_ENTRIES) {
    throw new PluginError(`disabled_tools has more than ${MAX_TOOL_ENTRIES} entries`, 400)
  }
  for (const tool of tools) {
    if (tool === '*' || !DISABLED_TOOL_NAME_RE.test(tool)) {
      throw new PluginError(`disabled_tools: "${tool}" is not a tool name (1–128 characters, no control characters)`, 400)
    }
  }
  return [...new Set(tools)].sort()
}

// ------------------------------------------------------------------- tiers

/** `mcp__<name>__`, the prefix of every tool the harness gets from a plugin. */
export function toolPrefix(pluginName: string): string {
  return `mcp__${pluginName}__`
}

/**
 * The tier of a plugin tool as the SDK names it: the explicit `tool_tiers`
 * entry, else `outward` (spec §8.1). Answers only for tools under one of the
 * plugins' prefixes, and `undefined` otherwise, so a plugin can never set the
 * tier of a tool it does not serve. Plugin names contain no `_`, so no
 * plugin's prefix is a prefix of another's or of `mcp__scadbuddy__`.
 */
export function pluginTierResolver(plugins: readonly Pick<RemotePlugin, 'name' | 'toolTiers'>[]): TierResolver {
  return (toolName) => {
    for (const plugin of plugins) {
      const prefix = toolPrefix(plugin.name)
      if (!toolName.startsWith(prefix)) continue
      const tool = toolName.slice(prefix.length)
      return Object.hasOwn(plugin.toolTiers, tool) ? plugin.toolTiers[tool] : 'outward'
    }
    return undefined
  }
}

// ------------------------------------------------------------------ rows

type Row = {
  name: string
  kind: PluginKind
  url: string
  auth_header: string | null
  last4: string | null
  created_at: Date
  updated_at: Date
  /** The plugin's `remote:<name>` rows of ai_access (#2171): its enabled flag, tiers and disabled tools. */
  access_rows: AccessRow[]
  /** Its ai_plugin_oauth row's public columns (#780), or null. */
  oauth: OAuthRow | null
}

type OAuthRow = {
  status: 'connected' | 'error'
  last_error: string | null
  issuer: string
  scope: string | null
  registration: 'dynamic' | 'configured'
  has_refresh: boolean
  access_expires_at: string | null
  connected_at: string
  grant_id: string
  plugin_url: string
}

/** The plugin's access as ai_access gives it. */
function access(row: Pick<Row, 'name' | 'access_rows'>): { enabled: boolean; toolTiers: Record<string, RiskTier>; disabledTools: string[] } {
  const source = remoteSource(row.name)
  return { enabled: sourceEnabled(source, row.access_rows), ...pluginOverrides(source, row.access_rows) }
}

/** `tool_tiers` and `disabled_tools` as ai_access tool rows: a disabled tool is `off`, whatever its tier. */
function toolRows(tiers: Record<string, RiskTier>, disabled: readonly string[]): Record<string, ToolAccess> {
  return { ...tiers, ...Object.fromEntries(disabled.map((tool) => [tool, 'off' as const])) }
}

function summary(row: Row): Omit<PluginSummary, 'opens'> {
  const { enabled, toolTiers, disabledTools } = access(row)
  return {
    name: row.name,
    kind: row.kind,
    url: row.url,
    enabled,
    auth_header: row.auth_header,
    secret_last4: row.last4,
    tool_tiers: toolTiers,
    disabled_tools: disabledTools,
    created_at: row.created_at.toISOString(),
    updated_at: row.updated_at.toISOString(),
    oauth: row.oauth
      ? {
          status: row.oauth.status,
          error: row.oauth.last_error,
          issuer: row.oauth.issuer,
          scope: row.oauth.scope,
          registration: row.oauth.registration,
          refreshes: row.oauth.has_refresh,
          expires_at: row.oauth.access_expires_at === null ? null : new Date(row.oauth.access_expires_at).toISOString(),
          connected_at: new Date(row.oauth.connected_at).toISOString(),
        }
      : null,
  }
}

/** A plugin whose endpoint passed the check, with the address that passed it. */
export type CheckedPlugin = { plugin: RemotePlugin; address: string }
export type LoadedPlugins = { plugins: CheckedPlugin[]; problems: string[] }

/**
 * The enabled plugins for one harness run: opened, and each endpoint checked
 * again (egress + https, `assertEndpointAllowed`) since its name may resolve
 * differently now than at save. The address checked is the one the forwarder
 * connects to. A plugin that fails either, or whose name Claude Code now
 * reserves (`reservedNameProblem`), is left out and named in `problems`; the
 * run goes ahead without it.
 */
export async function loadEnabledPlugins(store: Pick<PluginStoreLike, 'enabled'>, resolve: Resolver = systemResolver): Promise<LoadedPlugins> {
  const { plugins, problems } = await store.enabled()
  const allowed: CheckedPlugin[] = []
  for (const plugin of plugins) {
    const reserved = reservedNameProblem(plugin.name)
    if (reserved) {
      problems.push(`plugin ${plugin.name} was not loaded: ${reserved}`)
      continue
    }
    try {
      const [address] = await assertEndpointAllowed(plugin.url, resolve)
      if (address === undefined) throw new EgressError(`url host of ${plugin.name} resolves to no address`)
      allowed.push({ plugin, address })
    } catch (err) {
      if (!(err instanceof EgressError)) throw err
      problems.push(`plugin ${plugin.name} was not loaded: ${err.message}`)
    }
  }
  return { plugins: allowed, problems }
}

export type PluginStoreOptions = {
  /** Signs plugins in and gives their tokens (#780); without it a signed-in plugin is not loaded. */
  oauth?: PluginOAuth
}

/**
 * Gives an opened plugin its sign-in (#780): the token source, and the header as it is now.
 * Throws GrantError (sign in again) or OAuthError (the token endpoint is unreachable).
 */
async function signIn(
  oauth: PluginOAuth | undefined,
  row: Pick<Row, 'name' | 'url' | 'oauth'>,
  plugin: RemotePlugin,
): Promise<RemotePlugin> {
  if (!row.oauth || plugin.header) return plugin
  if (row.oauth.status === 'error') throw new GrantError(row.oauth.last_error ?? 'its sign-in failed')
  if (!oauth) throw new GrantError('OAuth sign-in is not available in this process')
  const source = oauth.source(row.name, row.url, row.oauth.grant_id)
  return { ...plugin, oauth: source, header: await source.header() }
}

/** `enabled`'s answer: each row opened (together: each signed-in plugin may wait on its token endpoint); one that fails is a problem. */
async function openEnabled<R extends { name: string }>(
  rows: readonly R[],
  open: (row: R) => Promise<RemotePlugin>,
): Promise<{ plugins: RemotePlugin[]; problems: string[] }> {
  const plugins: RemotePlugin[] = []
  const problems: string[] = []
  const opened = await Promise.all(
    rows.map(async (row) => {
      try {
        return { row, plugin: await open(row), err: undefined }
      } catch (err) {
        return { row, plugin: undefined, err: err as unknown }
      }
    }),
  )
  for (const one of opened) {
    const { row } = one
    if (one.plugin) {
      plugins.push(one.plugin)
      continue
    }
    const { err } = one
    if (err instanceof SealError) {
      problems.push(`plugin ${row.name} was not loaded: its secret cannot be opened (${err.message})`)
    } else if (err instanceof GrantError) {
      problems.push(`plugin ${row.name} was not loaded: it needs signing in again in Settings (${err.message})`)
    } else if (err instanceof OAuthError) {
      problems.push(`plugin ${row.name} was not loaded: its access token could not be renewed (${err.message})`)
    } else throw err
  }
  return { plugins, problems }
}

// ------------------------------------------------------------- the CRUD routes

/** A plugin as the API's routes answer it (backend api/crud_agent_plugins.py `PluginRow`). */
type WireRow = {
  name: string
  kind: PluginKind
  url: string
  auth_header: string | null
  kek_id: string | null
  last4: string | null
  created_at: string
  updated_at: string
  access_rows: AccessRow[]
  oauth: OAuthRow | null
  /** No secret, or one the API's key opens. */
  opens: boolean
}

/** `WireRow` with its header opened: `seal_error` when it cannot be, as `SealError`. */
type WireOpened = WireRow & { header: { name: string; value: string } | null; seal_error: string | null }

function rowOf(wire: WireRow): Row {
  return { ...wire, created_at: new Date(wire.created_at), updated_at: new Date(wire.updated_at) }
}

function summaryOf(wire: WireRow): PluginSummary {
  return { ...summary(rowOf(wire)), opens: wire.opens }
}

/** A plugin the API opened: its `seal_error` is a SealError. */
function openedOf(wire: WireOpened): RemotePlugin {
  if (wire.seal_error !== null) throw new SealError(wire.seal_error)
  const { toolTiers, disabledTools } = access(wire)
  return { name: wire.name, url: wire.url, toolTiers, disabledTools, ...(wire.header ? { header: wire.header } : {}) }
}

/** The route's reasons for a refusal the registry makes, and for no KEK on the API. */
const PLUGIN_REFUSED = 'plugin-refused'
const KEK_MISSING = 'kek-missing'

/**
 * The registry through the API's CRUD routes, area `plugins` (backend
 * api/crud_agent_plugins.py): one route per method, each its transaction on the API,
 * with the warm eviction in it. The checks that need no row (the name, the URL's form,
 * the header name, the secret, the tiers) run here first; the API seals the secret with
 * its KEK. A refusal of the API's is a PluginError.
 */
export class CrudPluginStore implements PluginStoreLike {
  readonly #call: ReturnType<typeof crudOf>
  readonly oauth: PluginOAuth | undefined

  constructor(target: CrudTarget, options: PluginStoreOptions = {}) {
    this.#call = crudOf(target)
    this.oauth = options.oauth
  }

  async #refusing<T>(call: () => Promise<T>): Promise<T> {
    try {
      return await call()
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute) {
        if (err.reason === PLUGIN_REFUSED && (err.status === 400 || err.status === 404 || err.status === 409)) {
          throw new PluginError(err.detail, err.status)
        }
        if (err.reason === KEK_MISSING) {
          throw new PluginError('a plugin secret cannot be saved: no key-encryption key is configured on the API (SCADBUDDY_SECRET_KEY_FILE, spec §9)', 503)
        }
      }
      throw err
    }
  }

  async list(): Promise<PluginSummary[]> {
    return (await this.#call<{ plugins: WireRow[] }>('the plugins', '/plugins')).plugins.map(summaryOf)
  }

  async get(name: string): Promise<PluginSummary | undefined> {
    if (!PLUGIN_NAME_RE.test(name)) return undefined
    const { plugin } = await this.#call<{ plugin: WireRow | null }>('a plugin', `/plugins/${segment(name, PLUGIN_NAME_RE, 'a plugin name')}`)
    return plugin ? summaryOf(plugin) : undefined
  }

  async create(input: PluginCreate): Promise<PluginSummary> {
    const name = validatePluginName(input.name)
    const url = normalisePluginUrl(input.url)
    const tiers = validateToolTiers(input.tool_tiers ?? {})
    const disabled = validateDisabledTools(input.disabled_tools ?? [])
    if (input.enabled) {
      throw new PluginError(
        'a plugin is registered disabled: run its connection test, review its tools, then enable it (issue #297)',
        400,
      )
    }
    let secret: { auth_header: string; secret: string } | undefined
    if (input.secret !== undefined) {
      secret = { auth_header: validateHeaderName(input.auth_header ?? DEFAULT_AUTH_HEADER), secret: validateSecret(input.secret) }
    } else if (input.auth_header) {
      throw new PluginError('auth_header needs a secret', 400)
    }
    const body = { name, url, ...secret, tools: toolRows(tiers, disabled), by: updatedBy() }
    return summaryOf(await this.#refusing(() => this.#call<WireRow>('a plugin registered', '/plugins', { body })))
  }

  async update(name: string, patch: PluginPatch, context?: AuditContext): Promise<PluginSummary> {
    if (!PLUGIN_NAME_RE.test(name)) throw new PluginError(`no plugin named "${name}"`, 404)
    const body: Record<string, unknown> = { by: updatedBy(context) }
    if (patch.url !== undefined) body.url = normalisePluginUrl(patch.url)
    if (patch.tool_tiers !== undefined) body.tool_tiers = validateToolTiers(patch.tool_tiers)
    if (patch.disabled_tools !== undefined) body.disabled_tools = validateDisabledTools(patch.disabled_tools)
    if (patch.enabled !== undefined) body.enabled = patch.enabled
    if (patch.auth_header !== undefined) body.auth_header = patch.auth_header === null ? null : validateHeaderName(patch.auth_header)
    if (patch.secret !== undefined) body.secret = patch.secret === null ? null : validateSecret(patch.secret)
    return summaryOf(
      await this.#refusing(() => this.#call<WireRow>('a plugin changed', `/plugins/${segment(name, PLUGIN_NAME_RE, 'a plugin name')}/update`, { body })),
    )
  }

  async setToolAccess(name: string, tool: string, value: ToolAccess | null, context?: AuditContext): Promise<PluginSummary> {
    if (!PLUGIN_NAME_RE.test(name)) throw new PluginError(`no plugin named "${name}"`, 404)
    // The one entry this write adds, checked by the validators.
    if (value === 'off') validateDisabledTools([tool])
    else if (value !== null) validateToolTiers({ [tool]: value })
    const body = { tool, access: value, by: updatedBy(context) }
    return summaryOf(
      await this.#refusing(() => this.#call<WireRow>("a plugin tool's access", `/plugins/${segment(name, PLUGIN_NAME_RE, 'a plugin name')}/tool-access`, { body })),
    )
  }

  async delete(name: string): Promise<boolean> {
    if (!PLUGIN_NAME_RE.test(name)) return false
    return (await this.#call<{ done: boolean }>('a plugin deleted', `/plugins/${segment(name, PLUGIN_NAME_RE, 'a plugin name')}/delete`, { method: 'POST' })).done
  }

  /** Throws SealError (the API cannot open the secret), and for a signed-in plugin GrantError or OAuthError. */
  async reveal(name: string): Promise<RemotePlugin | undefined> {
    if (!PLUGIN_NAME_RE.test(name)) return undefined
    const { plugin } = await this.#call<{ plugin: WireOpened | null }>('a plugin opened', `/plugins/${segment(name, PLUGIN_NAME_RE, 'a plugin name')}/reveal`, { method: 'POST' })
    return plugin ? signIn(this.oauth, plugin, openedOf(plugin)) : undefined
  }

  /** Every plugin `ai_access` turns on, opened by the API; one that cannot be opened is reported in `problems`, not loaded. */
  async enabled(): Promise<{ plugins: RemotePlugin[]; problems: string[] }> {
    const { plugins } = await this.#call<{ plugins: WireOpened[] }>('the enabled plugins', '/plugins/open-enabled', { method: 'POST' })
    return openEnabled(plugins, async (row) => signIn(this.oauth, row, openedOf(row)))
  }
}
