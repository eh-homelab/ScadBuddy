import type { Sql, TransactionSql } from 'postgres'
import { type AccessRow, SOURCE_ITEM, type ToolAccess } from '../../../src/access/model.js'
import type { AccessEdit, AccessRows } from '../../../src/access/store.js'
import { notifyWarmEvict } from '../../../src/sessions/warmEvict.js'

// Test-only: `ai_access` over SQL, the transaction the API's `…/access/write` runs (its warm
// eviction included), for the tests that run on a throwaway database. The agent itself goes
// through the API (src/access/store.ts CrudAccessRows).

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

/** `rows` (of any sources) as they stand after `edit`. */
function applyEdit(rows: readonly AccessRow[], edit: AccessEdit, by: string): AccessRow[] {
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

async function runEdit(tx: TransactionSql, edit: AccessEdit, by: string): Promise<void> {
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

export class SqlAccessRows implements AccessRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    if (sources.length === 0) return []
    return this.#sql<AccessRow[]>`
      SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${this.#sql(sources)} ORDER BY source, item`
  }

  async row(source: string, item: string): Promise<AccessRow | undefined> {
    const [row] = await this.#sql<AccessRow[]>`
      SELECT source, item, access, updated_by FROM ai_access WHERE source = ${source} AND item = ${item}`
    return row
  }

  async write(edits: readonly AccessEdit[], by: string, skipUnchanged: boolean): Promise<number[]> {
    let applied = edits.map((_, i) => i)
    await this.#sql.begin(async (tx) => {
      if (skipUnchanged) {
        const sources = [...new Set(edits.map((e) => e.source))]
        const current = await tx<AccessRow[]>`
          SELECT source, item, access, updated_by FROM ai_access WHERE source IN ${tx(sources)} FOR UPDATE`
        applied = applied.filter((i) => shape(applyEdit(current, edits[i]!, by), edits[i]!.source) !== shape(current, edits[i]!.source))
        if (applied.length === 0) return
      }
      await notifyWarmEvict(tx, '*')
      for (const i of applied) await runEdit(tx, edits[i]!, by)
    })
    return applied
  }
}
