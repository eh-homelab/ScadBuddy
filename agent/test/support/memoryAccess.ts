import { type AccessEdit, type AccessReader, type AccessRows, type AccessWriter, checkItem, checkSwitch } from '../../src/access/store.js'
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

/**
 * `ai_access` as the API's `…/access/*` routes keep it, for `AccessStore` without the API: a
 * write is all or nothing (a write touching `refuse` fails and changes nothing), and
 * `skipUnchanged` drops an edit that leaves its source's rows as they are.
 */
export class MemoryAccessRows implements AccessRows {
  stored: AccessRow[] = []
  refuse: string | undefined

  async rows(sources: readonly string[]): Promise<AccessRow[]> {
    const key = (r: AccessRow) => `${r.source}\u0000${r.item}`
    return this.stored
      .filter((r) => sources.includes(r.source))
      .sort((a, b) => (key(a) < key(b) ? -1 : 1))
      .map((r) => ({ ...r }))
  }

  async row(source: string, item: string): Promise<AccessRow | undefined> {
    const row = this.stored.find((r) => r.source === source && r.item === item)
    return row && { ...row }
  }

  async write(edits: readonly AccessEdit[], by: string, skipUnchanged: boolean): Promise<number[]> {
    const applied = edits.flatMap((e, i) =>
      !skipUnchanged || shape(applyEdit(this.stored, e, by), e.source) !== shape(this.stored, e.source) ? [i] : [],
    )
    if (applied.some((i) => edits[i]!.source === this.refuse)) throw new Error('refused for the test')
    let next = this.stored
    for (const i of applied) next = applyEdit(next, edits[i]!, by)
    this.stored = next
    return applied
  }
}
