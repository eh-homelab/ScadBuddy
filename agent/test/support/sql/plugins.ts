import type { Sql, TransactionSql } from 'postgres'
import { pluginOverrides, sourceEnabled } from '../../../src/access/resolve.js'
import { type AccessRow, remoteSource, SOURCE_ITEM, type ToolAccess } from '../../../src/access/model.js'
import { updatedBy } from '../../../src/access/store.js'
import type { AuditContext } from '../../../src/audit/log.js'
import type { RiskTier } from '../../../src/harness/permissions.js'
import { OAuthError } from '../../../src/plugins/oauth.js'
import { GrantError, type PluginOAuth } from '../../../src/plugins/oauthStore.js'
import {
  normalisePluginUrl,
  type PluginCreate,
  PluginError,
  type PluginKind,
  type PluginPatch,
  type PluginStoreLike,
  type PluginSummary,
  type RemotePlugin,
  DEFAULT_AUTH_HEADER,
  MAX_TOOL_ENTRIES,
  validateDisabledTools,
  validateHeaderName,
  validatePluginName,
  validateSecret,
  validateToolTiers,
} from '../../../src/plugins/registry.js'
import { type Envelope, type Kek, last4, openSecret, SealError, sealSecret } from '../../../src/secrets.js'
import { notifyWarmEvict } from './publisher.js'
import { forgetSource, writeEnabled, writeTools } from './access.js'

// Test-only: the plugin registry over SQL, sealing with the test's KEK as the API does, for
// the tests that run on a throwaway database. The agent itself goes through the API
// (src/plugins/registry.ts CrudPluginStore).

export type PluginStoreOptions = { oauth?: PluginOAuth }

/** The AAD the API seals a plugin's header value under (backend api/crud_agent_plugins.py). */
function pluginAad(name: string, url: string, header: string): string {
  return `ai_plugins:${JSON.stringify({ name, url, auth_header: header })}`
}

type Row = {
  name: string
  kind: PluginKind
  url: string
  auth_header: string | null
  secret_sealed: Buffer | null
  dek_sealed: Buffer | null
  kek_id: string | null
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

// The plugin's ai_access rows, read with its row. The prefix is remoteSource's.
const ACCESS_ROWS = `(SELECT coalesce(jsonb_agg(jsonb_build_object('source', a.source, 'item', a.item, 'access', a.access,
    'updated_by', a.updated_by)), '[]'::jsonb)
  FROM ai_access a WHERE a.source = '${remoteSource('')}' || ai_plugins.name) AS access_rows`

// The public columns of its sign-in (#780): never the sealed ones.
const OAUTH_ROW = `(SELECT jsonb_build_object('status', o.status, 'last_error', o.last_error, 'issuer', o.issuer,
    'scope', o.scope, 'registration', o.registration, 'has_refresh', o.has_refresh,
    'access_expires_at', o.access_expires_at, 'connected_at', o.connected_at, 'grant_id', o.grant_id,
    'plugin_url', o.plugin_url)
  FROM ai_plugin_oauth o WHERE o.plugin = ai_plugins.name) AS oauth`

const SUMMARY_COLUMNS = `name, kind, url, auth_header, kek_id, last4,
  created_at, updated_at, ${ACCESS_ROWS}, ${OAUTH_ROW}`

const ALL_COLUMNS = `*, ${ACCESS_ROWS}, ${OAUTH_ROW}`

/** The plugin's access as ai_access gives it. */
function access(row: Pick<Row, 'name' | 'access_rows'>): { enabled: boolean; toolTiers: Record<string, RiskTier>; disabledTools: string[] } {
  const source = remoteSource(row.name)
  return { enabled: sourceEnabled(source, row.access_rows), ...pluginOverrides(source, row.access_rows) }
}

/** `tool_tiers` and `disabled_tools` as ai_access tool rows: a disabled tool is `off`, whatever its tier. */
function toolRows(tiers: Record<string, RiskTier>, disabled: readonly string[]): Record<string, ToolAccess> {
  return { ...tiers, ...Object.fromEntries(disabled.map((tool) => [tool, 'off' as const])) }
}

async function selectRow(tx: TransactionSql, name: string): Promise<Row> {
  const [row] = await tx.unsafe<Row[]>(`SELECT ${ALL_COLUMNS} FROM ai_plugins WHERE name = $1`, [name])
  if (!row) throw new Error(`plugin ${name} is gone inside its own transaction`)
  return row
}

function summary(row: Omit<Row, 'secret_sealed' | 'dek_sealed'>, kek: Kek | undefined): PluginSummary {
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
    opens: row.kek_id === null || kek?.id === row.kek_id,
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

function openPlugin(row: Row, kek: Kek | undefined): RemotePlugin {
  const { toolTiers, disabledTools } = access(row)
  const plugin: RemotePlugin = { name: row.name, url: row.url, toolTiers, disabledTools }
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

export class PostgresPluginStore implements PluginStoreLike {
  private readonly sql: Sql
  private readonly kek: Kek | undefined
  readonly oauth: PluginOAuth | undefined
  constructor(sql: Sql, kek: Kek | undefined, options: PluginStoreOptions = {}) {
    this.sql = sql
    this.kek = kek
    this.oauth = options.oauth
  }

  async list(): Promise<PluginSummary[]> {
    const rows = await this.sql.unsafe<Row[]>(`SELECT ${SUMMARY_COLUMNS} FROM ai_plugins ORDER BY name`)
    return rows.map((row) => summary(row, this.kek))
  }

  async get(name: string): Promise<PluginSummary | undefined> {
    const [row] = await this.sql.unsafe<Row[]>(`SELECT ${SUMMARY_COLUMNS} FROM ai_plugins WHERE name = $1`, [name])
    return row ? summary(row, this.kek) : undefined
  }

  async create(input: PluginCreate): Promise<PluginSummary> {
    const kek = this.kek
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
    return this.sql.begin(async (tx) => {
      const rows = await tx`
        INSERT INTO ai_plugins (name, kind, url, auth_header, secret_sealed, dek_sealed, kek_id, last4)
        VALUES (${name}, 'remote_mcp', ${url}, ${sealed?.header ?? null},
                ${sealed?.envelope.secretSealed ?? null}, ${sealed?.envelope.dekSealed ?? null},
                ${sealed?.envelope.kekId ?? null}, ${sealed?.last4 ?? null})
        ON CONFLICT (name) DO NOTHING
        RETURNING name`
      if (rows.length === 0) throw new PluginError(`a plugin named "${name}" already exists`, 409)
      const source = remoteSource(name)
      // A row an earlier plugin of this name left behind is not this one's.
      await forgetSource(tx, source)
      await writeEnabled(tx, source, false, updatedBy())
      await writeTools(tx, source, toolRows(tiers, disabled), updatedBy())
      // A warm Claude Code was spawned with the plugins of its time (spec 2026-10-13 §5.3): every write here tells the caches (sessions/warmEvict.ts).
      await notifyWarmEvict(tx, '*')
      return summary(await selectRow(tx, name), this.kek)
    })
  }

  async update(name: string, patch: PluginPatch, context?: AuditContext): Promise<PluginSummary> {
    const kek = this.kek
    return this.sql.begin(async (tx) => {
      const [locked] = await tx`SELECT name FROM ai_plugins WHERE name = ${name} FOR UPDATE`
      if (!locked) throw new PluginError(`no plugin named "${name}"`, 404)
      const current = await selectRow(tx, name)
      const stored = access(current)
      const url = patch.url === undefined ? current.url : normalisePluginUrl(patch.url)
      const tiers = patch.tool_tiers === undefined ? stored.toolTiers : validateToolTiers(patch.tool_tiers)
      const disabled =
        patch.disabled_tools === undefined ? stored.disabledTools : validateDisabledTools(patch.disabled_tools)
      const enabled = patch.enabled ?? stored.enabled

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
      await tx`
        UPDATE ai_plugins SET
          url = ${url}, auth_header = ${secretColumns.auth_header}, secret_sealed = ${secretColumns.secret_sealed},
          dek_sealed = ${secretColumns.dek_sealed}, kek_id = ${secretColumns.kek_id},
          last4 = ${secretColumns.last4}, updated_at = now()
        WHERE name = ${name}`
      // The last listing was the old server's (#4994): the page lists none until the next test.
      if (url !== current.url) await tx`DELETE FROM ai_tool_listings WHERE source = ${remoteSource(name)}`
      // A sign-in was for the old resource; a header replaces it (#780): either way it goes.
      if (url !== current.url || (patch.secret !== undefined && patch.secret !== null)) {
        await tx`DELETE FROM ai_plugin_oauth_flows WHERE plugin = ${name}`
        await tx`DELETE FROM ai_plugin_oauth WHERE plugin = ${name}`
      }
      await this.writeAccess(tx, name, { tiers, disabled, enabled }, updatedBy(context), {
        enabled: patch.enabled !== undefined,
        tools: patch.tool_tiers !== undefined || patch.disabled_tools !== undefined,
      })
      return summary(await selectRow(tx, name), this.kek)
    })
  }

  async setToolAccess(name: string, tool: string, value: ToolAccess | null, context?: AuditContext): Promise<PluginSummary> {
    return this.sql.begin(async (tx) => {
      const [locked] = await tx`SELECT name FROM ai_plugins WHERE name = ${name} FOR UPDATE`
      if (!locked) throw new PluginError(`no plugin named "${name}"`, 404)
      const stored = access(await selectRow(tx, name))
      const source = remoteSource(name)
      // Each tool row as it stands, so only `tool`'s is attributed to this write (below).
      // `updated_at` as text: a Date would drop its microseconds.
      const before = await tx<{ item: string; access: string; updated_by: string; updated_at: string }[]>`
        SELECT item, access, updated_by, updated_at::text AS updated_at FROM ai_access WHERE source = ${source} AND item <> ${SOURCE_ITEM}`
      // No prototype: a tool named `__proto__` is an own key.
      const tiers = Object.assign(Object.create(null) as Record<string, string>, stored.toolTiers)
      delete tiers[tool]
      const disabled = stored.disabledTools.filter((t) => t !== tool)
      if (value === 'off') disabled.push(tool)
      else if (value !== null) tiers[tool] = value
      // In the page's words: the validators' refusal names fields the page never shows.
      if (disabled.length > MAX_TOOL_ENTRIES) {
        throw new PluginError(`At most ${MAX_TOOL_ENTRIES} tools of plugin "${name}" can be off: reset one of them first`, 400)
      }
      if (Object.keys(tiers).length > MAX_TOOL_ENTRIES) {
        throw new PluginError(
          `At most ${MAX_TOOL_ENTRIES} tools of plugin "${name}" can have an access other than off or the default: reset one of them first`,
          400,
        )
      }
      await this.writeAccess(
        tx,
        name,
        { tiers: validateToolTiers(tiers), disabled: validateDisabledTools(disabled), enabled: stored.enabled },
        updatedBy(context),
        { enabled: false, tools: true },
      )
      // writeTools wrote every tool row as new: the rows this write left as they were get their own author and time back.
      const kept = before.filter((r) => r.item !== tool)
      if (kept.length > 0) {
        await tx`
          UPDATE ai_access a SET updated_by = k.updated_by, updated_at = k.updated_at
          FROM jsonb_to_recordset(${tx.json(kept as never)}) AS k(item text, access text, updated_by text, updated_at timestamptz)
          WHERE a.source = ${source} AND a.item = k.item AND a.access = k.access`
      }
      return summary(await selectRow(tx, name), this.kek)
    })
  }

  /**
   * The plugin's access, written to ai_access (`write` names which rows). The old
   * ai_plugins columns are not written. Then the warm eviction.
   */
  private async writeAccess(
    tx: TransactionSql,
    name: string,
    next: { tiers: Record<string, RiskTier>; disabled: string[]; enabled: boolean },
    by: string,
    write: { enabled: boolean; tools: boolean },
  ): Promise<void> {
    await tx`
      UPDATE ai_plugins SET updated_at = now() WHERE name = ${name}`
    const source = remoteSource(name)
    if (write.enabled) await writeEnabled(tx, source, next.enabled, by)
    if (write.tools) await writeTools(tx, source, toolRows(next.tiers, next.disabled), by)
    // A warm Claude Code was spawned with the plugins of its time (spec 2026-10-13 §5.3).
    await notifyWarmEvict(tx, '*')
  }

  async delete(name: string): Promise<boolean> {
    return this.sql.begin(async (tx) => {
      const rows = await tx`DELETE FROM ai_plugins WHERE name = ${name}`
      if (rows.count === 0) return false
      await forgetSource(tx, remoteSource(name))
      await notifyWarmEvict(tx, '*')
      return true
    })
  }

  /** Throws SealError, and for a signed-in plugin GrantError or OAuthError (`signIn`). */
  async reveal(name: string): Promise<RemotePlugin | undefined> {
    const kek = this.kek
    const [row] = await this.sql.unsafe<Row[]>(`SELECT ${ALL_COLUMNS} FROM ai_plugins WHERE name = $1`, [name])
    return row ? signIn(this.oauth, row, openPlugin(row, kek)) : undefined
  }

  /** Every plugin ai_access turns on, opened; one that cannot be opened is reported in `problems`, not loaded. */
  async enabled(): Promise<{ plugins: RemotePlugin[]; problems: string[] }> {
    const kek = this.kek
    const rows = await this.sql.unsafe<Row[]>(`SELECT ${ALL_COLUMNS} FROM ai_plugins ORDER BY name`)
    return openEnabled(
      rows.filter((r) => access(r).enabled),
      (row) => signIn(this.oauth, row, openPlugin(row, kek)),
    )
  }
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
