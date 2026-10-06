import type { CustomizerSchema, Param, ParamGroup, ParamValue } from '../api/types'
import { normalizeHex } from './format'

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

/**
 * Each colour parameter's extruder, as the backend numbers them (`jobs.extruder_order`):
 * parameters sharing a colour are one part, so they share the first one's extruder.
 * A colour the geometry never uses gets no part; only the rendered job knows that.
 */
export function extrudersOf(schema: CustomizerSchema, values: ParamValues): Map<string, number> {
  const byColour = new Map<string, number>()
  const extruders = new Map<string, number>()
  for (const name of colorParamNames(schema)) {
    const colour = normalizeHex(String(values[name] ?? ''))
    if (!byColour.has(colour)) byColour.set(colour, byColour.size + 1)
    extruders.set(name, byColour.get(colour)!)
  }
  return extruders
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

export type CheckedValue = { ok: true; value: ParamValue } | { ok: false; message: string }

const HEX = /^#?([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i
/** An uploaded asset's id: the sha256 of its content (#204). */
const ASSET_ID = /^[0-9a-f]{64}$/

/**
 * A string parameter's length as OpenSCAD's `len()` counts it: in code points, so an
 * emoji is one character, not the two UTF-16 units `String.length` counts (#920).
 */
export function textLength(value: string): number {
  return Array.from(value).length
}

/**
 * #254 — whether `value` is one the parameter's own widget could produce, and the value
 * as that widget would hand it to `onChange` (a select's option in its own type, a
 * colour normalised). An agent's value goes through this before the same `onChange`
 * a keystroke does, so it cannot put a value on screen no field would have.
 */
export function checkParamValue(param: Param, value: ParamValue): CheckedValue {
  const label = `"${param.name}"`
  switch (param.type) {
    case 'number':
    case 'integer':
    case 'slider': {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        return { ok: false, message: `${label} takes a number.` }
      }
      if (param.type === 'integer' && !Number.isInteger(value)) {
        return { ok: false, message: `${label} takes a whole number.` }
      }
      if (param.min != null && value < param.min) {
        return { ok: false, message: `${label} is at least ${param.min}.` }
      }
      if (param.max != null && value > param.max) {
        return { ok: false, message: `${label} is at most ${param.max}.` }
      }
      return { ok: true, value }
    }
    case 'boolean':
      return typeof value === 'boolean'
        ? { ok: true, value }
        : { ok: false, message: `${label} takes true or false.` }
    case 'select': {
      const options = param.options ?? []
      const picked = options.find((option) => String(option.value) === String(value))
      return picked
        ? { ok: true, value: picked.value }
        : {
            ok: false,
            message: `${label} is one of ${options.map((option) => JSON.stringify(option.value)).join(', ')}.`,
          }
    }
    case 'color':
      return typeof value === 'string' && HEX.test(value.trim())
        ? { ok: true, value: normalizeHex(value) }
        : { ok: false, message: `${label} takes a hex colour such as "#FF8800".` }
    case 'file': {
      if (typeof value !== 'string') return { ok: false, message: `${label} takes a file name.` }
      const samples = param.samples ?? []
      if (value === '' || samples.includes(value) || ASSET_ID.test(value)) return { ok: true, value }
      return {
        ok: false,
        message:
          `${label} takes one of the template's samples (${samples.map((s) => JSON.stringify(s)).join(', ') || 'none'}), ` +
          'an uploaded asset id, or "" to clear it. Uploading a file is for the user.',
      }
    }
    case 'font':
    case 'string': {
      if (typeof value !== 'string') return { ok: false, message: `${label} takes text.` }
      if (param.max_length != null && textLength(value) > param.max_length) {
        return { ok: false, message: `${label} is at most ${param.max_length} characters.` }
      }
      return { ok: true, value }
    }
  }
}
