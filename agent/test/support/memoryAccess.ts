import { type AccessReader, type AccessWriter, checkItem, checkSwitch } from '../../src/access/store.js'
import { type AccessRow, SOURCE_ITEM } from '../../src/access/model.js'

/** An in-memory AccessStore for tests with no Postgres; `reads.n` counts `rows()` calls. */
export function memoryAccess(initial: AccessRow[] = []): { access: AccessReader & AccessWriter; rows: AccessRow[]; reads: { n: number } } {
  const rows: AccessRow[] = [...initial]
  const reads = { n: 0 }
  const by = (c?: { actor: { kind: string; id: string } }): string => (c ? `${c.actor.kind}:${c.actor.id}` : 'system')
  const put = (source: string, item: string, access: string, updated_by: string): void => {
    const i = rows.findIndex((r) => r.source === source && r.item === item)
    const row = { source, item, access, updated_by }
    if (i < 0) rows.push(row)
    else rows[i] = row
  }
  const drop = (keep: (r: AccessRow) => boolean): void => {
    const kept = rows.filter(keep)
    rows.length = 0
    rows.push(...kept)
  }
  const access: AccessReader & AccessWriter = {
    async rows(sources) {
      reads.n += 1
      return rows.filter((r) => sources.includes(r.source)).map((r) => ({ ...r }))
    },
    async row(source, item) {
      reads.n += 1
      const row = rows.find((r) => r.source === source && r.item === item)
      return row && { ...row }
    },
    async setItem(source, item, access, context) {
      checkItem(source, item, access)
      put(source, item, access, by(context))
    },
    async resetItem(source, item) {
      drop((r) => !(r.source === source && r.item === item))
    },
    async setEnabled(source, enabled, context) {
      checkSwitch(source)
      put(source, SOURCE_ITEM, enabled ? 'on' : 'off', by(context))
    },
    async replaceTools(source, tools, context) {
      drop((r) => !(r.source === source && r.item !== SOURCE_ITEM))
      for (const [item, tier] of Object.entries(tools)) put(source, item, tier, by(context))
    },
    async replaceToolsOf(bySource, context) {
      for (const [source, tools] of Object.entries(bySource)) await access.replaceTools(source, tools, context)
    },
    async forget(source) {
      drop((r) => r.source !== source)
    },
  }
  return { access, rows, reads }
}

/** Rows as the migration copies old overrides: disabled wins over a tier. */
export function rowsFromOverrides(
  source: string,
  tool_tiers: Record<string, string>,
  disabled_tools: string[],
  updated_by = 'browser:browser',
): AccessRow[] {
  const out: AccessRow[] = disabled_tools.map((item) => ({ source, item, access: 'off', updated_by }))
  for (const [item, access] of Object.entries(tool_tiers)) {
    if (!disabled_tools.includes(item)) out.push({ source, item, access, updated_by })
  }
  return out
}
