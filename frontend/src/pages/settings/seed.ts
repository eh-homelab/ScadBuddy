import type { FieldName } from './fields'

export type Draft = Partial<Record<FieldName, string>>

/** How many times each field has been typed into. Only ever counts up. */
export type Edits = ReadonlyMap<FieldName, number>

/** A request whose answer seeds the form: the fields it seeds, and the edit counts
 *  when it was sent. A field typed into since then keeps what was typed (#767). */
export type Seed = { names: 'all' | readonly FieldName[]; since: Edits }

/** Every field the queued seeds cover, each with the edit count it may still have.
 *  A field two seeds cover takes the later request's count, the larger one. */
export function pendingFields(seeds: readonly Seed[], all: () => readonly FieldName[]): Map<FieldName, number> {
  const since = new Map<FieldName, number>()
  for (const seed of seeds) {
    for (const name of seed.names === 'all' ? all() : seed.names) {
      since.set(name, Math.max(since.get(name) ?? 0, seed.since.get(name) ?? 0))
    }
  }
  return since
}

/** The fields of `since` typed into after their seed's request was sent. */
export function editedSince(since: ReadonlyMap<FieldName, number>, edits: Edits): Set<FieldName> {
  return new Set([...since].filter(([name, count]) => (edits.get(name) ?? 0) !== count).map(([name]) => name))
}

/** `current` with each of `values` applied, except the fields in `edited`. */
export function seedDraft(current: Draft, values: Draft, edited: ReadonlySet<FieldName>): Draft {
  const next = { ...current }
  for (const [name, value] of Object.entries(values) as [FieldName, string][]) {
    if (!edited.has(name)) next[name] = value
  }
  return next
}
