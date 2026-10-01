import type { CustomizerSchema, Param, ParamValue } from '../api/types'
import { getPath, isParamValue, splitInputs, type JsonObject } from '../lib/inputs'
import { allParams, type ParamValues } from '../lib/params'
import type { HostElement } from './elements'

interface BindingContext {
  schema: CustomizerSchema
  inputs: JsonObject
}

interface Binding {
  name: string
  bind: string
  param: Param
  value: ParamValue
  /** Set when the value at `bind` is not of the parameter's type, so the default is shown. */
  mistyped?: string
}

/** What an `<sb-param>` is bound to, or why it cannot be; the value is checked against the
 *  parameter's type (`typeof initial`), falling back to the default. */
export function bindingOf(element: HostElement, context: BindingContext): Binding | string {
  const name = element.getAttribute('name') ?? ''
  const file = element.getAttribute('file') ?? 'model.scad'
  // An empty `bind` is no binding: the default, never a '' key in the inputs.
  const bind = element.getAttribute('bind') || `params.${name}`
  if (file !== 'model.scad') return `Only model.scad has parameters in host API v1, not ${file}.`
  const param = allParams(context.schema).find((candidate) => candidate.name === name)
  if (!param) return `model.scad has no parameter “${name}”.`
  // `initial` may be null or missing (OpenSCAD exports some parameters without one); the
  // widget then gets '' rather than undefined, as `defaultValues` gives it (lib/params).
  const initial = param.initial as ParamValue | null | undefined
  const bound = getPath(context.inputs, bind)
  const typed = isParamValue(initial) ? typeof bound === typeof initial : isParamValue(bound)
  if (bound === undefined || typed) {
    return { name, bind, param, value: ((bound === undefined ? initial : bound) ?? '') as ParamValue }
  }
  const expected = isParamValue(initial) ? typeof initial : `${param.type} value`
  return {
    name,
    bind,
    param,
    value: initial ?? '',
    mistyped: `${bind} holds ${JSON.stringify(bound)}, not a ${expected} for “${name}”; showing its default.`,
  }
}

/** The values the widgets show: `params`, with every `<sb-param>`'s bound value applied,
 *  so colour parameters bound outside `params` are numbered with the rest. A parameter is
 *  keyed by its default binding (`params.<name>`) when an element has it: an element
 *  rebound elsewhere then never overrides it, whatever the elements' order. */
export function effectiveValues(
  elements: readonly HostElement[],
  context: BindingContext,
): ParamValues {
  const values: ParamValues = { ...splitInputs(context.inputs).params }
  const byDefault = new Set<string>()
  for (const element of elements) {
    if (element.localName !== 'sb-param') continue
    const binding = bindingOf(element, context)
    if (typeof binding === 'string' || binding.mistyped) continue
    const isDefault = binding.bind === `params.${binding.name}`
    if (!isDefault && byDefault.has(binding.name)) continue
    if (isDefault) byDefault.add(binding.name)
    values[binding.name] = binding.value
  }
  return values
}
