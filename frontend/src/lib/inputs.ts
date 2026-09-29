import type { ParamValue } from '../api/types'
import type { ParamValues } from './params'

/** Template inputs (spec 2026-09-27 §4.3): JSON the template owns. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export type JsonObject = { [key: string]: Json }
/** Every inputs key except `params`: a template UI's own state, never rendered. */
export type InputsExtra = JsonObject

export const NO_EXTRA: InputsExtra = Object.freeze({}) as InputsExtra

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isParamValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

export function splitInputs(
  raw: Record<string, unknown> | null | undefined,
  fallback: ParamValues = {},
): { params: ParamValues; extra: InputsExtra } {
  if (!raw) return { params: fallback, extra: NO_EXTRA }
  const { params, ...rest } = raw
  const valid =
    isJsonObject(params) && Object.values(params).every(isParamValue)
      ? (params as ParamValues)
      : fallback
  return { params: valid, extra: Object.keys(rest).length ? (rest as InputsExtra) : NO_EXTRA }
}

export function joinInputs(params: ParamValues, extra: InputsExtra): JsonObject {
  return { ...extra, params }
}

export function mergePatch(target: Json | undefined, patch: Json): Json {
  if (!isJsonObject(patch)) return patch
  const result: JsonObject = isJsonObject(target) ? { ...target } : {}
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) delete result[key]
    else result[key] = mergePatch(result[key], value)
  }
  return result
}

export function getPath(root: Json, path: string): Json | undefined {
  let node: Json | undefined = root
  for (const key of path.split('.')) {
    if (!isJsonObject(node)) return undefined
    node = node[key]
  }
  return node
}

export function setPath(root: JsonObject, path: string, value: Json): JsonObject {
  const [head = '', ...rest] = path.split('.')
  if (rest.length === 0) return { ...root, [head]: value }
  const child = root[head]
  return { ...root, [head]: setPath(isJsonObject(child) ? child : {}, rest.join('.'), value) }
}
