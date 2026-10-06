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

export function isParamValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

/**
 * `params` and the rest (`extra`) of stored inputs. `v` stays in `extra`, so `extra`
 * is `{ v: 0 }`, not empty, for a template without a UI once anything is loaded:
 * whether a template has a UI is its `ui` declaration, never `extra` being non-empty.
 * Anything but an object reads as no inputs.
 */
export function splitInputs(
  raw: Record<string, unknown> | null | undefined,
  fallback: ParamValues = {},
): { params: ParamValues; extra: InputsExtra } {
  if (!isJsonObject(raw)) return { params: fallback, extra: NO_EXTRA }
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

/** Whether two JSON values are equal, whatever the order of their object keys. */
export function sameJson(a: Json | undefined, b: Json | undefined): boolean {
  if (a === b) return true
  if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((item, i) => sameJson(item, b[i]))
  if (!isJsonObject(a) || !isJsonObject(b)) return false
  const keys = Object.keys(a)
  return keys.length === Object.keys(b).length && keys.every((key) => Object.hasOwn(b, key) && sameJson(a[key], b[key]))
}

/** Keys a patch from a template's UI never writes: they would reach a prototype. */
const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

export function mergePatch(target: Json | undefined, patch: Json): Json {
  if (!isJsonObject(patch)) return patch
  const result: JsonObject = isJsonObject(target) ? { ...target } : {}
  for (const [key, value] of Object.entries(patch)) {
    if (UNSAFE_KEYS.has(key)) continue
    if (value === null) delete result[key]
    else result[key] = mergePatch(result[key], value)
  }
  return result
}

/** The value at a dotted path of own object keys; arrays are not indexed. */
export function getPath(root: Json, path: string): Json | undefined {
  let node: Json | undefined = root
  for (const key of path.split('.')) {
    if (!isJsonObject(node) || !Object.hasOwn(node, key)) return undefined
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
