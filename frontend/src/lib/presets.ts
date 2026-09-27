import type { CustomizerSchema, ParamPreset, ParamValue } from '../api/types'
import { allParams, defaultValues, diffFromDefaults, type ParamValues } from './params'

export interface AppliedPreset {
  /** The whole set of values on screen once the preset is applied. */
  values: ParamValues
  /** The preset's parameters this template no longer has, which were left out. */
  skipped: string[]
}

/**
 * A preset holds only the values it sets, so it is applied over the template's
 * defaults: a default the template changes later still reaches every preset that never
 * touched it. A value for a parameter the template has since dropped is skipped rather
 * than sent — the render would refuse it.
 */
export function applyPreset(schema: CustomizerSchema, preset: ParamPreset): AppliedPreset {
  const known = new Set(allParams(schema).map((param) => param.name))
  const values: ParamValues = defaultValues(schema)
  const skipped: string[] = []
  for (const [name, value] of Object.entries(preset.params)) {
    if (known.has(name)) values[name] = value
    else skipped.push(name)
  }
  return { values, skipped }
}

/** What a preset saved from `values` holds: only the values that differ from the defaults. */
export function presetParams(
  schema: CustomizerSchema,
  values: ParamValues,
): Record<string, ParamValue> {
  return Object.fromEntries(diffFromDefaults(schema, values).map((diff) => [diff.name, diff.value]))
}
