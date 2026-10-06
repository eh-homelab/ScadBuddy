import type { Sql } from 'postgres'
import { isLoopbackPeer } from '../http/origins.js'
import { assertHostAllowed, EgressError, type Resolver, systemResolver } from '../http/egress.js'
import { RISK_TIERS, type RiskTier, type TierResolver } from '../harness/permissions.js'
import { type Envelope, type Kek, last4, openSecret, rewrap, SealError, sealSecret } from '../secrets.js'

// The plugin registry (issue #297): "provide an endpoint and we'll add the
// plugin/skills to the harness". Stored in Postgres, `ai_plugins`
// (src/db/migrations/<timestamp>_plugins.sql; spec §9, "All AI state lives in the #241
// database"). No file is written.
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
// URL is https unless every address it resolves to is loopback (spec §8.4's one
// exception, "loopback, for local development and tests"). The host must also
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
// name must be given the secret again. No route returns it (spec §8.6).

export const PLUGIN_KINDS = ['remote_mcp'] as const
export type PluginKind = (typeof PLUGIN_KINDS)[number]

/** 2–32 characters: lower-case letters, digits, single hyphens; starts with a letter. */
export const PLUGIN_NAME_RE = /^[a-z][a-z0-9-]{0,30}[a-z0-9]$/

/**
 * Names a plugin may not take: the in-process ScadBuddy server (#251's
 * `mcp__scadbuddy__*`) and the headless browser plugin (#349), whose tools have
 * tiers of their own, plus the vendor names, plus the server names Claude Code
 * 2.1.283 and 2.1.287 treat specially and drop or never offer tools from
 * (`workspace`, `computer-use`, `claude-in-chrome`, `hearthbot`, `ide`; 2.1.287
 * also reserves `widgets`, but only in a hosted or CLAUDE_CODE_REMOTE run).
 */
export const RESERVED_PLUGIN_NAMES: ReadonlySet<string> = new Set([
  'scadbuddy',
  'playwright',
  'claude',
  'anthropic',
  'plugin',
  'workspace',
  'computer-use',
  'claude-in-chrome',
  'hearthbot',
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
  /** Which KEK sealed the secret; null when there is none. Never put in a route body. */
  kekId: string | null
}

export type PluginCreate = {
  name: string
  url: string
  enabled?: boolean
  auth_header?: string | null
  secret?: string
  tool_tiers?: Record<string, RiskTier>
  disabled_tools?: string[]
}

/** Omitted fields are kept. `secret: null` removes the auth header and its value. */
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
  toolTiers: Record<string, RiskTier>
  disabledTools: string[]
}

/** The registry as routes and the session manager see it. */
export type PluginRepo = {
  list(): Promise<PluginSummary[]>
  get(name: string): Promise<PluginSummary | undefined>
  create(input: PluginCreate, kek: Kek | undefined): Promise<PluginSummary>
  update(name: string, patch: PluginPatch, kek: Kek | undefined): Promise<PluginSummary>
  delete(name: string): Promise<boolean>
  /** Opens one plugin's secret. Throws SealError on a wrong key or an altered row. */
  reveal(name: string, kek: Kek | undefined): Promise<RemotePlugin | undefined>
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
    throw new PluginError(`url must be https (or http to loopback), not ${url.protocol}//`, 400)
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
 * allowed only when every address the host resolves to is loopback. Resolves
 * to the checked addresses; the forwarder connects to the first of them and
 * does not resolve the name again.
 */
export async function assertEndpointAllowed(url: string, resolve: Resolver = systemResolver): Promise<string[]> {
  const addresses = await assertHostAllowed(url, resolve, 'url', 'an MCP endpoint')
  const parsed = new URL(url)
  if (parsed.protocol === 'https:') return addresses
  if (!addresses.every((a) => isLoopbackPeer(a))) {
    throw new EgressError(
      `url must be https: plain http is allowed only to loopback, and ${parsed.hostname} is not (spec §8.4)`,
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
  const out: Record<string, RiskTier> = {}
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
    if (!DISABLED_TOOL_NAME_RE.test(tool)) {
      throw new PluginError(`disabled_tools: "${tool}" is not a tool name (1–128 characters, no control characters)`, 400)
    }
  }
  return [...new Set(tools)].sort()
}

/**
 * AAD for a plugin's sealed header value: the table, the name, and the columns
 * that decide where the value is sent. JSON so no URL can forge a boundary.
 */
export function pluginAad(name: string, url: string, header: string): string {
  return `ai_plugins:${JSON.stringify({ name, url, auth_header: header })}`
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

// ------------------------------------------------------------------ storage

type Row = {
  name: string
  kind: PluginKind
  url: string
  enabled: boolean
  auth_header: string | null
  secret_sealed: Buffer | null
  dek_sealed: Buffer | null
  kek_id: string | null
  last4: string | null
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
  created_at: Date
  updated_at: Date
}

const SUMMARY_COLUMNS = `name, kind, url, enabled, auth_header, kek_id, last4, tool_tiers, disabled_tools,
  created_at, updated_at`

function summary(row: Omit<Row, 'secret_sealed' | 'dek_sealed'>): PluginSummary {
  return {
    name: row.name,
    kind: row.kind,
    url: row.url,
    enabled: row.enabled,
    auth_header: row.auth_header,
    secret_last4: row.last4,
    tool_tiers: row.tool_tiers,
    disabled_tools: row.disabled_tools,
    created_at: row.created_at.toISOString(),
    updated_at: row.updated_at.toISOString(),
    kekId: row.kek_id,
  }
}

type Sealed = { header: string; envelope: Envelope; last4: string } | null

function sealFor(name: string, url: string, header: string, secret: string, kek: Kek | undefined): Sealed {
  if (!kek) {
    throw new PluginError(
      'a plugin secret cannot be saved: no key-encryption key is configured (SCADBUDDY_SECRET_KEY_FILE, spec §9)',
      503,
    )
  }
  const value = validateSecret(secret)
  return { header, envelope: sealSecret(kek, value, pluginAad(name, url, header)), last4: last4(value) }
}

export function openPlugin(row: Row, kek: Kek | undefined): RemotePlugin {
  const plugin: RemotePlugin = {
    name: row.name,
    url: row.url,
    toolTiers: row.tool_tiers,
    disabledTools: row.disabled_tools,
  }
  if (row.secret_sealed && row.dek_sealed && row.kek_id && row.auth_header) {
    if (!kek) throw new SealError('no key-encryption key is configured to open the plugin secret')
    const value = openSecret(
      kek,
      { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
      pluginAad(row.name, row.url, row.auth_header),
    )
    plugin.header = { name: row.auth_header, value }
  }
  return plugin
}

/** A plugin whose endpoint passed the check, with the address that passed it. */
export type CheckedPlugin = { plugin: RemotePlugin; address: string }
export type LoadedPlugins = { plugins: CheckedPlugin[]; problems: string[] }

/**
 * The enabled plugins for one harness run: opened, and each endpoint checked
 * again (egress + https, `assertEndpointAllowed`) since its name may resolve
 * differently now than at save. The address checked is the one the forwarder
 * connects to. A plugin that fails either is left out and named in
 * `problems`; the run goes ahead without it.
 */
export async function loadEnabledPlugins(
  store: Pick<PluginStore, 'enabled'>,
  kek: Kek | undefined,
  resolve: Resolver = systemResolver,
): Promise<LoadedPlugins> {
  const { plugins, problems } = await store.enabled(kek)
  const allowed: CheckedPlugin[] = []
  for (const plugin of plugins) {
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

export type RewrapResult = { rewrapped: number; failed: number }

export class PluginStore implements PluginRepo {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  async list(): Promise<PluginSummary[]> {
    const rows = await this.sql.unsafe<Row[]>(`SELECT ${SUMMARY_COLUMNS} FROM ai_plugins ORDER BY name`)
    return rows.map(summary)
  }

  async get(name: string): Promise<PluginSummary | undefined> {
    const [row] = await this.sql.unsafe<Row[]>(`SELECT ${SUMMARY_COLUMNS} FROM ai_plugins WHERE name = $1`, [name])
    return row ? summary(row) : undefined
  }

  async create(input: PluginCreate, kek: Kek | undefined): Promise<PluginSummary> {
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
    let sealed: Sealed = null
    if (input.secret !== undefined) {
      const header = validateHeaderName(input.auth_header ?? DEFAULT_AUTH_HEADER)
      sealed = sealFor(name, url, header, input.secret, kek)
    } else if (input.auth_header) {
      throw new PluginError('auth_header needs a secret', 400)
    }
    const rows = await this.sql<Row[]>`
      INSERT INTO ai_plugins (name, kind, url, enabled, auth_header, secret_sealed, dek_sealed, kek_id, last4,
                              tool_tiers, disabled_tools)
      VALUES (${name}, 'remote_mcp', ${url}, false, ${sealed?.header ?? null},
              ${sealed?.envelope.secretSealed ?? null}, ${sealed?.envelope.dekSealed ?? null},
              ${sealed?.envelope.kekId ?? null}, ${sealed?.last4 ?? null},
              ${this.sql.json(tiers)}, ${disabled})
      ON CONFLICT (name) DO NOTHING
      RETURNING *`
    const row = rows[0]
    if (!row) throw new PluginError(`a plugin named "${name}" already exists`, 409)
    return summary(row)
  }

  async update(name: string, patch: PluginPatch, kek: Kek | undefined): Promise<PluginSummary> {
    return this.sql.begin(async (tx) => {
      const [current] = await tx<Row[]>`SELECT * FROM ai_plugins WHERE name = ${name} FOR UPDATE`
      if (!current) throw new PluginError(`no plugin named "${name}"`, 404)
      const url = patch.url === undefined ? current.url : normalisePluginUrl(patch.url)
      const tiers = patch.tool_tiers === undefined ? current.tool_tiers : validateToolTiers(patch.tool_tiers)
      const disabled =
        patch.disabled_tools === undefined ? current.disabled_tools : validateDisabledTools(patch.disabled_tools)
      const enabled = patch.enabled ?? current.enabled

      // The secret. Changing the URL or the header name re-binds it, which
      // needs the value typed in again (the stored one is not sent somewhere
      // new); `secret: null` removes it.
      let secretColumns: {
        auth_header: string | null
        secret_sealed: Buffer | null
        dek_sealed: Buffer | null
        kek_id: string | null
        last4: string | null
      }
      if (patch.secret === null) {
        if (patch.auth_header) throw new PluginError('auth_header needs a secret', 400)
        secretColumns = { auth_header: null, secret_sealed: null, dek_sealed: null, kek_id: null, last4: null }
      } else if (patch.secret !== undefined) {
        const header = validateHeaderName(patch.auth_header ?? current.auth_header ?? DEFAULT_AUTH_HEADER)
        const sealed = sealFor(name, url, header, patch.secret, kek)
        secretColumns = {
          auth_header: header,
          secret_sealed: sealed?.envelope.secretSealed ?? null,
          dek_sealed: sealed?.envelope.dekSealed ?? null,
          kek_id: sealed?.envelope.kekId ?? null,
          last4: sealed?.last4 ?? null,
        }
      } else {
        const header = patch.auth_header === undefined ? current.auth_header : patch.auth_header
        if (current.auth_header === null) {
          if (header) throw new PluginError('auth_header needs a secret', 400)
        } else if (url !== current.url || header !== current.auth_header) {
          throw new PluginError(
            'changing url or auth_header needs the secret again (or secret: null to remove it): ' +
              'the stored one is not sent to a new destination',
            409,
          )
        }
        secretColumns = {
          auth_header: current.auth_header,
          secret_sealed: current.secret_sealed,
          dek_sealed: current.dek_sealed,
          kek_id: current.kek_id,
          last4: current.last4,
        }
      }
      const [row] = await tx<Row[]>`
        UPDATE ai_plugins SET
          url = ${url}, enabled = ${enabled}, tool_tiers = ${tx.json(tiers)}, disabled_tools = ${disabled},
          auth_header = ${secretColumns.auth_header}, secret_sealed = ${secretColumns.secret_sealed},
          dek_sealed = ${secretColumns.dek_sealed}, kek_id = ${secretColumns.kek_id},
          last4 = ${secretColumns.last4}, updated_at = now()
        WHERE name = ${name}
        RETURNING *`
      if (!row) throw new Error('UPDATE ... RETURNING returned no row')
      return summary(row)
    })
  }

  async delete(name: string): Promise<boolean> {
    const rows = await this.sql`DELETE FROM ai_plugins WHERE name = ${name}`
    return rows.count > 0
  }

  async reveal(name: string, kek: Kek | undefined): Promise<RemotePlugin | undefined> {
    const [row] = await this.sql<Row[]>`SELECT * FROM ai_plugins WHERE name = ${name}`
    return row ? openPlugin(row, kek) : undefined
  }

  /** Every enabled plugin, opened; one that cannot be opened is reported in `problems`, not loaded. */
  async enabled(kek: Kek | undefined): Promise<{ plugins: RemotePlugin[]; problems: string[] }> {
    const rows = await this.sql<Row[]>`SELECT * FROM ai_plugins WHERE enabled ORDER BY name`
    const plugins: RemotePlugin[] = []
    const problems: string[] = []
    for (const row of rows) {
      try {
        plugins.push(openPlugin(row, kek))
      } catch (err) {
        if (!(err instanceof SealError)) throw err
        problems.push(`plugin ${row.name} was not loaded: its secret cannot be opened (${err.message})`)
      }
    }
    return { plugins, problems }
  }

  /** Key rotation, as CredentialStore.rewrapFrom does for the Claude credential. */
  async rewrapFrom(previous: Kek, current: Kek): Promise<RewrapResult> {
    const result: RewrapResult = { rewrapped: 0, failed: 0 }
    if (previous.id === current.id) return result
    const rows = await this.sql<Row[]>`SELECT * FROM ai_plugins WHERE kek_id = ${previous.id}`
    for (const row of rows) {
      if (!row.secret_sealed || !row.dek_sealed || !row.kek_id || !row.auth_header) continue
      let next: Envelope
      try {
        next = rewrap(
          previous,
          current,
          { secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id },
          pluginAad(row.name, row.url, row.auth_header),
        )
      } catch (err) {
        if (err instanceof SealError) {
          result.failed++
          continue
        }
        throw err
      }
      const updated = await this.sql`
        UPDATE ai_plugins SET dek_sealed = ${next.dekSealed}, kek_id = ${next.kekId}
        WHERE name = ${row.name} AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    }
    return result
  }
}
