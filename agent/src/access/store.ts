import type { Sql, TransactionSql } from 'postgres'
import { type AuditContext, type AuditSink, SYSTEM_ACTOR } from '../audit/log.js'
import { notifyWarmEvict } from '../sessions/warmEvict.js'
import { LEGACY_SOURCES, legacySettings, type RegistryTool } from './legacy.js'
import { type AccessRow, isToolAccess, SOURCE_ITEM, type ToolAccess } from './model.js'

export type AccessReader = { rows(sources: readonly string[]): Promise<AccessRow[]> }

export type AccessWriter = {
  setItem(source: string, item: string, access: ToolAccess, context?: AuditContext): Promise<void>
  resetItem(source: string, item: string, context?: AuditContext): Promise<void>
  setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void>
  replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void>
  /** `replaceTools` for several sources in one transaction; a source whose rows would not change is left alone. */
  replaceToolsOf(bySource: Readonly<Record<string, Readonly<Record<string, ToolAccess>>>>, context?: AuditContext): Promise<void>
  forget(source: string): Promise<void>
}

/** A write the model does not allow (400). Nothing was stored. */
export class AccessError extends Error {
  readonly status = 400
}

const SOURCE_RE = /^(builtin:(scadbuddy|browser|http|own-plugin)|remote:[a-z][a-z0-9-]*|package:.+)$/

function checkSource(source: string): void {
  if (!SOURCE_RE.test(source)) throw new AccessError(`unknown access source ${JSON.stringify(source)}`)
}

function checkTool(item: string, access: unknown): asserts access is ToolAccess {
  if (item === '' || item === SOURCE_ITEM) throw new AccessError(`${JSON.stringify(item)} is not a tool name`)
  if (!isToolAccess(access)) throw new AccessError(`a tool takes off, read, write or outward, not ${JSON.stringify(access)}`)
}

/** `<actor.kind>:<actor.id>`; `system` without a context. */
export function updatedBy(context?: AuditContext): string {
  return context ? `${context.actor.kind}:${context.actor.id}` : 'system'
}

/** Replaces every tool row of `source` (not `*`) with `tools`. The caller sends the eviction. */
export async function writeTools(tx: TransactionSql, source: string, tools: Readonly<Record<string, ToolAccess>>, by: string): Promise<void> {
  await tx`DELETE FROM ai_access WHERE source = ${source} AND item <> ${SOURCE_ITEM}`
  for (const [item, access] of Object.entries(tools)) {
    await tx`INSERT INTO ai_access (source, item, access, updated_by) VALUES (${source}, ${item}, ${access}, ${by})`
  }
}

export async function writeEnabled(tx: TransactionSql, source: string, enabled: boolean, by: string): Promise<void> {
  await tx`
    INSERT INTO ai_access (source, item, access, updated_by) VALUES (${source}, ${SOURCE_ITEM}, ${enabled ? 'on' : 'off'}, ${by})
    ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()`
}

/** Deletes everything stored for `source`, its tool listing included. */
export async function forgetSource(tx: TransactionSql, source: string): Promise<void> {
  await tx`DELETE FROM ai_access WHERE source = ${source}`
  await tx`DELETE FROM ai_tool_listings WHERE source = ${source}`
}

/** One source's change: its tool rows replaced, its rows forgotten, or single items set (`null` deletes). */
type Edit =
  | { source: string; kind: 'items'; items: Readonly<Record<string, string | null>> }
  | { source: string; kind: 'replace'; tools: Readonly<Record<string, ToolAccess>> }
  | { source: string; kind: 'forget' }

/** `rows` (of any sources) as they stand after `edit`. */
function applyEdit(rows: readonly AccessRow[], edit: Edit, by: string): AccessRow[] {
  const mine = (r: AccessRow): boolean => r.source === edit.source
  if (edit.kind === 'forget') return rows.filter((r) => !mine(r))
  if (edit.kind === 'replace') {
    const kept = rows.filter((r) => !mine(r) || r.item === SOURCE_ITEM)
    return [...kept, ...Object.entries(edit.tools).map(([item, access]) => ({ source: edit.source, item, access, updated_by: by }))]
  }
  let out = [...rows]
  for (const [item, access] of Object.entries(edit.items)) {
    out = out.filter((r) => !(mine(r) && r.item === item))
    if (access !== null) out.push({ source: edit.source, item, access, updated_by: by })
  }
  return out
}

/** The source's rows as `item=access`, sorted: what a write changes (`updated_by` aside). */
function shape(rows: readonly AccessRow[], source: string): string {
  return rows
    .filter((r) => r.source === source)
    .map((r) => `${r.item}=${r.access}`)
    .sort()
    .join('\n')
}

async function runEdit(tx: TransactionSql, edit: Edit, by: string): Promise<void> {
  if (edit.kind === 'forget') return forgetSource(tx, edit.source)
  if (edit.kind === 'replace') return writeTools(tx, edit.source, edit.tools, by)
  for (const [item, access] of Object.entries(edit.items)) {
    if (access === null) await tx`DELETE FROM ai_access WHERE source = ${edit.source} AND item = ${item}`
    else {
      await tx`
        INSERT INTO ai_access (source, item, access, updated_by) VALUES (${edit.source}, ${item}, ${access}, ${by})
        ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()`
    }
  }
}

const toolsDetail = (edits: readonly Edit[]): string =>
  edits.map((e) => `${e.source}/* tools = ${JSON.stringify(e.kind === 'replace' ? e.tools : {})}`).join('; ')

export class AccessStore implements AccessReader, AccessWriter {
  private readonly sql: Sql
  private readonly audit: AuditSink | undefined
  private readonly registryTools: readonly RegistryTool[] | undefined
  /** `registryTools` (the registry's tools and tiers) lets a write keep `builtin_tools.scadbuddy` and the camera key (access/legacy.ts). */
  constructor(sql: Sql, audit?: AuditSink, registryTools?: readonly RegistryTool[]) {
    this.sql = sql
    this.audit = audit
    this.registryTools = registryTools
  }

  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    if (sources.length === 0) return []
    return this.sql<AccessRow[]>`
      SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${this.sql(sources)} ORDER BY source, item`
  }

  async setItem(source: string, item: string, access: ToolAccess, context?: AuditContext): Promise<void> {
    checkSource(source)
    checkTool(item, access)
    await this.write(context, [{ source, kind: 'items', items: { [item]: access } }], () => `${source}/${item} = ${JSON.stringify(access)}`)
  }

  async resetItem(source: string, item: string, context?: AuditContext): Promise<void> {
    checkSource(source)
    if (item === '' || item === SOURCE_ITEM) throw new AccessError(`${JSON.stringify(item)} is not a tool name`)
    await this.write(context, [{ source, kind: 'items', items: { [item]: null } }], () => `${source}/${item} reset`)
  }

  async setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void> {
    checkSource(source)
    const access = enabled ? 'on' : 'off'
    await this.write(context, [{ source, kind: 'items', items: { [SOURCE_ITEM]: access } }], () => `${source}/${SOURCE_ITEM} = ${JSON.stringify(access)}`)
  }

  async replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void> {
    checkSource(source)
    for (const [item, access] of Object.entries(tools)) checkTool(item, access)
    await this.write(context, [{ source, kind: 'replace', tools }], toolsDetail)
  }

  async replaceToolsOf(bySource: Readonly<Record<string, Readonly<Record<string, ToolAccess>>>>, context?: AuditContext): Promise<void> {
    for (const [source, tools] of Object.entries(bySource)) {
      checkSource(source)
      for (const [item, access] of Object.entries(tools)) checkTool(item, access)
    }
    const edits: Edit[] = Object.entries(bySource).map(([source, tools]) => ({ source, kind: 'replace', tools }))
    await this.write(context, edits, toolsDetail, { skipUnchanged: true })
  }

  async forget(source: string): Promise<void> {
    checkSource(source)
    await this.write(undefined, [{ source, kind: 'forget' }], () => `${source} forgotten`)
  }

  /**
   * The old ai_settings keys of the sources `edits` touch, as they will stand, written before
   * the ai_access rows: the mirror trigger an older replica's writes need
   * (20261027T0514Z_access_settings_mirror.sql) then fires first, and the rows written after
   * it win. Drop next release (#4828).
   */
  private async writeLegacy(tx: TransactionSql, edits: readonly Edit[], by: string): Promise<void> {
    const touched = new Set(edits.map((e) => e.source).filter((s) => LEGACY_SOURCES.includes(s)))
    if (touched.size === 0) return
    // One such write at a time, so each derives the keys from the rows the last one left.
    await tx`SELECT pg_advisory_xact_lock(hashtext('ai_access legacy keys'))`
    let rows: AccessRow[] = await tx<AccessRow[]>`
      SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${tx(LEGACY_SOURCES)}`
    for (const edit of edits) rows = applyEdit(rows, edit, by)
    const settings = legacySettings(rows, touched, this.registryTools)
    if (settings.length === 0) return
    const stored = new Map(
      (await tx<{ key: string; value: unknown }[]>`SELECT key, value FROM ai_settings WHERE key IN ${tx(settings.map((s) => s.key))}`).map(
        (r) => [r.key, JSON.stringify(r.value)],
      ),
    )
    for (const { key, value, absent } of settings) {
      const now = JSON.stringify(value)
      if ((stored.get(key) ?? JSON.stringify(absent)) === now) continue
      await tx`
        INSERT INTO ai_settings (key, value) VALUES (${key}, ${tx.json(value as never)})
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
    }
  }

  /**
   * One transaction with the eviction (a warm process holds its tools), then the audit row.
   * With `skipUnchanged`, an edit that would leave its source's rows as they are is dropped,
   * and a write left with none sends nothing and records nothing.
   */
  private async write(
    context: AuditContext | undefined,
    edits: readonly Edit[],
    detailOf: (edits: readonly Edit[]) => string,
    options: { skipUnchanged?: boolean } = {},
  ): Promise<void> {
    const startedAt = new Date()
    const by = updatedBy(context)
    let applied: readonly Edit[] = edits
    let failure: unknown
    try {
      await this.sql.begin(async (tx) => {
        if (options.skipUnchanged) {
          const sources = [...new Set(edits.map((e) => e.source))]
          const current = await tx<AccessRow[]>`
            SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${tx(sources)} FOR UPDATE`
          applied = edits.filter((e) => shape(applyEdit(current, e, by), e.source) !== shape(current, e.source))
          if (applied.length === 0) return
        }
        // Told first: the notification is queued in this transaction, so a failing write drops it.
        await notifyWarmEvict(tx, '*')
        await this.writeLegacy(tx, applied, by)
        for (const edit of applied) await runEdit(tx, edit, by)
      })
    } catch (err) {
      failure = err
    }
    if (failure === undefined && applied.length === 0) return
    await this.audit?.record({
      kind: 'settings',
      action: 'ai_access',
      surface: context?.surface ?? 'system',
      actor: context?.actor ?? SYSTEM_ACTOR,
      clientIp: context?.clientIp,
      outcome: failure === undefined ? 'ok' : 'error',
      detail: `${detailOf(applied)}${failure === undefined ? '' : ` (failed: ${failure instanceof Error ? failure.message : String(failure)})`}`,
      startedAt,
      finishedAt: new Date(),
    })
    if (failure !== undefined) throw failure
  }
}
