import type { ReactNode } from 'react'
import type { CustomizerSchema, FontFamily, ParamValue } from '../api/types'
import { ParamWidget } from '../components/widgets/ParamWidget'
import { getPath, setPath, splitInputs, type JsonObject } from '../lib/inputs'
import { allParams, extrudersOf } from '../lib/params'
import type { HostElement } from './elements'
import type { UiSlot } from './types'

export interface ElementContext {
  schema: CustomizerSchema
  slug: string
  version?: string
  fonts: FontFamily[]
  inputs: JsonObject
  onInputs: (next: JsonObject) => void
  slot: UiSlot
  preview: ReactNode
  generate: ReactNode
}

function Problem({ children }: { children: ReactNode }) {
  return <p role="alert" className="px-3 py-2 text-[12px] text-warn">{children}</p>
}

function isParamValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

function BoundParam({ element, context }: { element: HostElement; context: ElementContext }) {
  const name = element.getAttribute('name') ?? ''
  const file = element.getAttribute('file') ?? 'model.scad'
  const bind = element.getAttribute('bind') ?? `params.${name}`
  if (file !== 'model.scad') return <Problem>Only model.scad has parameters in host API v1, not {file}.</Problem>
  const param = allParams(context.schema).find((candidate) => candidate.name === name)
  if (!param) return <Problem>model.scad has no parameter “{name}”.</Problem>
  const bound = getPath(context.inputs, bind)
  const value = isParamValue(bound) ? bound : (param.initial as ParamValue)
  const { params } = splitInputs(context.inputs)
  // `data-param`, as ParameterPanel's rows carry it: the agent's highlight (#254) finds
  // the parameter it just changed by it (`findParamRow`).
  return (
    <div data-param={name}>
      <ParamWidget
        param={param}
        value={value}
        slug={context.slug}
        version={context.version}
        fonts={context.fonts}
        extruder={extrudersOf(context.schema, params).get(name)}
        onChange={(next) => context.onInputs(setPath(context.inputs, bind, next))}
      />
    </div>
  )
}

export function HostElementContent({ element, context }: { element: HostElement; context: ElementContext }) {
  switch (element.localName) {
    case 'sb-param':
      return <BoundParam element={element} context={context} />
    case 'sb-preview':
      // In the panel slot the host's own preview is beside the panel already.
      return context.slot === 'page' ? context.preview : null
    case 'sb-generate':
      return context.generate
    default:
      return null
  }
}
