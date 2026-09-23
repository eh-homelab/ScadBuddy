import type { ModelSchema, Param, ParamValue } from '../api/types'

export type ParamValues = Record<string, ParamValue>

export function allParams(schema: ModelSchema): Param[] {
  return schema.groups.flatMap((group) => group.params)
}

export function defaultValues(schema: ModelSchema): ParamValues {
  return Object.fromEntries(allParams(schema).map((param) => [param.name, param.initial]))
}

/** Colour parameters in schema order — extruder 1 is the first one (spec §7). */
export function colorParamNames(schema: ModelSchema): string[] {
  return allParams(schema)
    .filter((param) => param.type === 'color')
    .map((param) => param.name)
}

export function colorsFrom(schema: ModelSchema, values: ParamValues): string[] {
  return colorParamNames(schema).map((name) => String(values[name] ?? '#9AA4B2'))
}

export interface ParamDiff {
  name: string
  caption: string
  value: ParamValue
  initial: ParamValue
}

/** Parameters whose value differs from the model's defaults. */
export function diffFromDefaults(schema: ModelSchema, values: ParamValues): ParamDiff[] {
  return allParams(schema)
    .filter((param) => {
      const value = values[param.name]
      return value !== undefined && value !== param.initial
    })
    .map((param) => ({
      name: param.name,
      caption: param.caption ?? param.name,
      value: values[param.name] as ParamValue,
      initial: param.initial,
    }))
}

export function sameValues(a: ParamValues, b: ParamValues): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  for (const key of keys) {
    if (a[key] !== b[key]) return false
  }
  return true
}
