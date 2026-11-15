import type { Sql } from 'postgres'
import { type CrudTarget, crudOf } from '../api/crudStores.js'

export type ActivityKindDecl = {
  key: string
  label: string
  facets: { name: string; type: 'keyword' | 'number' | 'bool' | 'time'; label: string }[]
}

/** The agent's activity kinds (spec 2026-10-09 §4), from its operation kinds: upserted at
 *  start, and every row not declared now retired, never deleted (old rows keep a label). */
export async function upsertActivityKinds(sql: Sql, kinds: ActivityKindDecl[]): Promise<void> {
  await sql.begin(async (tx) => {
    for (const k of kinds) {
      await tx`
        INSERT INTO ai_activity_kinds (key, label, facets) VALUES (${k.key}, ${k.label}, ${tx.json(k.facets as never)})
        ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, facets = EXCLUDED.facets, retired_at = NULL, updated_at = now()`
    }
    await tx`UPDATE ai_activity_kinds SET retired_at = now() WHERE retired_at IS NULL AND key <> ALL(${kinds.map((k) => k.key)})`
  })
}

/** `upsertActivityKinds` through the API's CRUD route, area `activity-kinds` (#5299 step 5): one transaction there. */
export async function crudUpsertActivityKinds(target: CrudTarget, kinds: ActivityKindDecl[]): Promise<void> {
  await crudOf(target)('the activity kinds', '/activity-kinds', { body: { kinds } })
}

export function activityKindsOf(kinds: readonly { name: string; label?: string }[]): ActivityKindDecl[] {
  return kinds.map((k) => ({ key: `agent.${k.name}`, label: k.label ?? k.name.replaceAll('_', ' '), facets: [] }))
}
