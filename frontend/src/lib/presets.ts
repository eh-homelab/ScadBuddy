import type { CustomizerSchema, ParamPreset, ParamValue } from '../api/types'
import { joinInputs, splitInputs, type InputsExtra, type JsonObject } from './inputs'
import { allParams, defaultValues, diffFromDefaults, type ParamValues } from './params'

export interface AppliedPreset {
  /** The whole set of values on screen once the preset is applied. */
  values: ParamValues
  /** The preset's parameters this template no longer has, which were left out. */
  skipped: string[]
  /** The preset's UI state (spec 2026-09-27 §4.3); empty for a params-only preset. */
  extra: InputsExtra
}

/**
 * A preset holds only the values it sets, so it is applied over the template's
 * defaults: a default the template changes later still reaches every preset that never
 * touched it. A value for a parameter the template has since dropped is skipped rather
 * than sent — the render would refuse it.
 */
export function applyPreset(schema: CustomizerSchema, preset: ParamPreset): AppliedPreset {
  const known = new Set(allParams(schema).map((param) => param.name))
  const { params, extra } = splitInputs(preset.inputs, preset.params)
  const values: ParamValues = defaultValues(schema)
  const skipped: string[] = []
  for (const [name, value] of Object.entries(params)) {
    if (known.has(name)) values[name] = value
    else skipped.push(name)
  }
  return { values, skipped, extra }
}

/** What a preset saved from `values` holds: only the values that differ from the defaults. */
export function presetParams(
  schema: CustomizerSchema,
  values: ParamValues,
): Record<string, ParamValue> {
  return Object.fromEntries(diffFromDefaults(schema, values).map((diff) => [diff.name, diff.value]))
}

/** What a preset saved from the page holds: the changed values, plus the UI state. */
export function presetInputs(
  schema: CustomizerSchema,
  values: ParamValues,
  extra: InputsExtra,
): JsonObject {
  return joinInputs(presetParams(schema, values), extra)
}
