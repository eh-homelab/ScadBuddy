import type { CustomizerSchema, Param, ParamValue } from '../api/types'
import { getPath, splitInputs, type JsonObject } from '../lib/inputs'
import { allParams, type ParamValues } from '../lib/params'
import type { HostElement } from './elements'

interface BindingContext {
  schema: CustomizerSchema
  inputs: JsonObject
}

function isParamValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
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
  const bind = element.getAttribute('bind') ?? `params.${name}`
  if (file !== 'model.scad') return `Only model.scad has parameters in host API v1, not ${file}.`
  const param = allParams(context.schema).find((candidate) => candidate.name === name)
  if (!param) return `model.scad has no parameter “${name}”.`
  const initial = param.initial as ParamValue
  const bound = getPath(context.inputs, bind)
  const typed = isParamValue(initial) ? typeof bound === typeof initial : isParamValue(bound)
  if (bound === undefined || typed) return { name, bind, param, value: bound === undefined ? initial : (bound as ParamValue) }
  return {
    name,
    bind,
    param,
    value: initial,
    mistyped: `${bind} holds ${JSON.stringify(bound)}, not a ${typeof initial} for “${name}”; showing its default.`,
  }
}

/** The values the widgets show: `params`, with every `<sb-param>`'s bound value applied,
 *  so colour parameters bound outside `params` are numbered with the rest. */
export function effectiveValues(
  elements: readonly HostElement[],
  context: BindingContext,
): ParamValues {
  const values: ParamValues = { ...splitInputs(context.inputs).params }
  for (const element of elements) {
    if (element.localName !== 'sb-param') continue
    const binding = bindingOf(element, context)
    if (typeof binding !== 'string' && !binding.mistyped) values[binding.name] = binding.value
  }
  return values
}
