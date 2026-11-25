import { ApiError } from '../../api/client'
import { hasValue, type PropertyValue } from './api'

// #7703 — a row's defined keys as fields of its own form: their order, the save's patch, and a refusal's keys.

export type Sub = { type?: string | string[]; title?: string; description?: string; enum?: unknown[] }
export type Values = Record<string, PropertyValue | undefined>
/** A refused save's keys (422 `missing_required_properties`): `missing` and `invalid`. */
export type FieldErrors = { missing: string[]; invalid: { key: string; reason: string }[] }


/** A refused save's keys, when the refusal is 422 `missing_required_properties`; else null. */
export function fieldErrorsOf(cause: unknown): FieldErrors | null {
  if (!(cause instanceof ApiError) || cause.problem.code !== 'missing_required_properties') return null
  const missing = Array.isArray(cause.problem.missing) ? (cause.problem.missing as string[]) : []
  const invalid = Array.isArray(cause.problem.invalid) ? (cause.problem.invalid as { key: string; reason: string }[]) : []
  return { missing, invalid }
}

/** The sentence a refused save shows above its fields. */
export function refusalText(errors: FieldErrors): string {
  const keys = [...errors.missing, ...errors.invalid.map((one) => one.key)]
  return `Fill the required properties first: ${keys.join(', ')}.`
}

export const propertiesOf = (schema: Record<string, unknown>): Record<string, Sub> =>
  schema.properties && typeof schema.properties === 'object' ? (schema.properties as Record<string, Sub>) : {}
export const requiredOf = (schema: Record<string, unknown>): string[] => (Array.isArray(schema.required) ? schema.required.filter((k): k is string => typeof k === 'string') : [])
export const typeOf = (sub: Sub): string => ([] as unknown[]).concat(sub.type ?? 'string').find((t) => t !== 'null') as string

/** The schema's keys as a form shows them: required ones first, then the rest, each by key (spec §6.3b). */
export function definedKeys(schema: Record<string, unknown>): string[] {
  const required = new Set(requiredOf(schema))
  const keys = Object.keys(propertiesOf(schema)).sort()
  return [...keys.filter((k) => required.has(k)), ...keys.filter((k) => !required.has(k))]
}

/** A list value as sent: its items trimmed, empty ones left out. */
const cleaned = (value: PropertyValue | undefined): PropertyValue | undefined =>
  Array.isArray(value) ? value.map((item) => item.trim()).filter(Boolean) : value

/**
 * The `properties` a save sends for the defined keys: a changed value, `null` for a key that
 * had a value and now has none, and nothing for a key left as it was or still empty.
 */
export function propertiesPatch(schema: Record<string, unknown>, before: Values, draft: Values): Record<string, PropertyValue | null> {
  const patch: Record<string, PropertyValue | null> = {}
  for (const key of Object.keys(propertiesOf(schema))) {
    const now = cleaned(draft[key])
    const was = before[key]
    if (!hasValue(now)) {
      if (hasValue(was)) patch[key] = null
      continue
    }
    if (JSON.stringify(now) !== JSON.stringify(was)) patch[key] = now as PropertyValue
  }
  return patch
}

