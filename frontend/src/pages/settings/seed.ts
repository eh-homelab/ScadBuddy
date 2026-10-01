import type { FieldName } from './fields'

export type Draft = Partial<Record<FieldName, string>>

/** `current` with each of `values` applied, except the fields in `edited`: those were
 *  typed into after the seed was asked for, and the seed's effect can land after the
 *  keystroke (#767). */
export function seedDraft(current: Draft, values: Draft, edited: ReadonlySet<FieldName>): Draft {
  const next = { ...current }
  for (const [name, value] of Object.entries(values) as [FieldName, string][]) {
    if (!edited.has(name)) next[name] = value
  }
  return next
}
