import { type CrudTarget, crudOf } from '../api/crudStores.js'

export type ActivityKindDecl = {
  key: string
  label: string
  facets: { name: string; type: 'keyword' | 'number' | 'bool' | 'time'; label: string }[]
}

/**
 * The agent's activity kinds (spec 2026-10-09 §4), from its operation kinds, through the API's
 * CRUD route, area `activity-kinds` (#5299 step 5): upserted at start in one transaction there,
 * and every row not declared now retired, never deleted (old rows keep a label).
 */
export async function crudUpsertActivityKinds(target: CrudTarget, kinds: ActivityKindDecl[]): Promise<void> {
  await crudOf(target)('the activity kinds', '/activity-kinds', { body: { kinds } })
}

export function activityKindsOf(kinds: readonly { name: string; label?: string }[]): ActivityKindDecl[] {
  return kinds.map((k) => ({ key: `agent.${k.name}`, label: k.label ?? k.name.replaceAll('_', ' '), facets: [] }))
}
