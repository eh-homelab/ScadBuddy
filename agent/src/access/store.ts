import type { Sql, TransactionSql } from 'postgres'
import { type AuditContext, type AuditSink, SYSTEM_ACTOR } from '../audit/log.js'
import { notifyWarmEvict } from '../sessions/warmEvict.js'
import { type AccessRow, isToolAccess, SOURCE_ITEM, type ToolAccess } from './model.js'

export type AccessReader = { rows(sources: readonly string[]): Promise<AccessRow[]> }

export type AccessWriter = {
  setItem(source: string, item: string, access: ToolAccess, context?: AuditContext): Promise<void>
  resetItem(source: string, item: string, context?: AuditContext): Promise<void>
  setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void>
  replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void>
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

export class AccessStore implements AccessReader, AccessWriter {
  private readonly sql: Sql
  private readonly audit: AuditSink | undefined
  constructor(sql: Sql, audit?: AuditSink) {
    this.sql = sql
    this.audit = audit
  }

  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    if (sources.length === 0) return []
    return this.sql<AccessRow[]>`
      SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${this.sql(sources)} ORDER BY source, item`
  }

  async setItem(source: string, item: string, access: ToolAccess, context?: AuditContext): Promise<void> {
    checkSource(source)
    checkTool(item, access)
    await this.write(`${source}/${item} = ${JSON.stringify(access)}`, context, async (tx, by) => {
      await tx`
        INSERT INTO ai_access (source, item, access, updated_by) VALUES (${source}, ${item}, ${access}, ${by})
        ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()`
    })
  }

  async resetItem(source: string, item: string, context?: AuditContext): Promise<void> {
    checkSource(source)
    if (item === '' || item === SOURCE_ITEM) throw new AccessError(`${JSON.stringify(item)} is not a tool name`)
    await this.write(`${source}/${item} reset`, context, async (tx) => {
      await tx`DELETE FROM ai_access WHERE source = ${source} AND item = ${item}`
    })
  }

  async setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void> {
    checkSource(source)
    await this.write(`${source}/${SOURCE_ITEM} = ${JSON.stringify(enabled ? 'on' : 'off')}`, context, (tx, by) => writeEnabled(tx, source, enabled, by))
  }

  async replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void> {
    checkSource(source)
    for (const [item, access] of Object.entries(tools)) checkTool(item, access)
    await this.write(`${source}/* tools = ${JSON.stringify(tools)}`, context, (tx, by) => writeTools(tx, source, tools, by))
  }

  async forget(source: string): Promise<void> {
    checkSource(source)
    await this.write(`${source} forgotten`, undefined, async (tx) => forgetSource(tx, source))
  }

  /** One transaction with the eviction (a warm process holds its tools), then the audit row. */
  private async write(detail: string, context: AuditContext | undefined, fn: (tx: TransactionSql, by: string) => Promise<void>): Promise<void> {
    const startedAt = new Date()
    let failure: unknown
    try {
      await this.sql.begin(async (tx) => {
        // Told first: the notification is queued in this transaction, so a failing write drops it.
        await notifyWarmEvict(tx, '*')
        await fn(tx, updatedBy(context))
      })
    } catch (err) {
      failure = err
    }
    await this.audit?.record({
      kind: 'settings',
      action: 'ai_access',
      surface: context?.surface ?? 'system',
      actor: context?.actor ?? SYSTEM_ACTOR,
      clientIp: context?.clientIp,
      outcome: failure === undefined ? 'ok' : 'error',
      detail: `${detail}${failure === undefined ? '' : ` (failed: ${failure instanceof Error ? failure.message : String(failure)})`}`,
      startedAt,
      finishedAt: new Date(),
    })
    if (failure !== undefined) throw failure
  }
}
