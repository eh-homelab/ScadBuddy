import type { CustomizerSchema, Param, ParamGroup, ParamValue } from '../api/types'

export type ParamValues = Record<string, ParamValue>

export const UNGROUPED = 'Parameters'

export function allParams(schema: CustomizerSchema): Param[] {
  return schema.parameters ?? []
}

/**
 * Bucket the flat `parameters` list into the panel's tabs, in the order
 * `schema.groups` records — which is the order the `/* [Group] *\/` comments appear in
 * the source, not alphabetical.
 */
export function groupsOf(schema: CustomizerSchema): ParamGroup[] {
  const buckets = new Map<string, Param[]>()
  for (const name of schema.groups ?? []) {
    buckets.set(name || UNGROUPED, [])
  }
  for (const param of allParams(schema)) {
    const name = param.group || UNGROUPED
    const bucket = buckets.get(name)
    if (bucket) bucket.push(param)
    else buckets.set(name, [param])
  }
  return [...buckets].filter(([, params]) => params.length > 0).map(([name, params]) => ({
    name,
    params,
  }))
}

export function defaultValues(schema: CustomizerSchema): ParamValues {
  return Object.fromEntries(
    allParams(schema)
      .filter((param) => param.initial !== null && param.initial !== undefined)
      .map((param) => [param.name, param.initial as ParamValue]),
  )
}

/** Colour parameters in schema order — extruder 1 is the first one (spec §7). */
export function colorParamNames(schema: CustomizerSchema): string[] {
  return allParams(schema)
    .filter((param) => param.type === 'color')
    .map((param) => param.name)
}

export function colorsFrom(schema: CustomizerSchema, values: ParamValues): string[] {
  return colorParamNames(schema).map((name) => String(values[name] ?? '#9AA4B2'))
}

export interface ParamDiff {
  name: string
  caption: string
  value: ParamValue
  initial: ParamValue
}

/** Parameters whose value differs from the model's defaults. */
export function diffFromDefaults(schema: CustomizerSchema, values: ParamValues): ParamDiff[] {
  return allParams(schema)
    .filter((param) => {
      const value = values[param.name]
      return value !== undefined && value !== param.initial
    })
    .map((param) => ({
      name: param.name,
      caption: param.caption ?? param.name,
      value: values[param.name] as ParamValue,
      initial: (param.initial ?? '') as ParamValue,
    }))
}

export function sameValues(a: ParamValues, b: ParamValues): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  for (const key of keys) {
    if (a[key] !== b[key]) return false
  }
  return true
}
