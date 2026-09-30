import type { ReactNode } from 'react'
import type { CustomizerSchema, FontFamily } from '../api/types'
import { ParamWidget } from '../components/widgets/ParamWidget'
import { setPath, type JsonObject } from '../lib/inputs'
import { extrudersOf, type ParamValues } from '../lib/params'
import { bindingOf } from './bindings'
import type { HostElement } from './elements'
import type { UiSlot } from './types'

export interface ElementContext {
  schema: CustomizerSchema
  slug: string
  version?: string
  fonts: FontFamily[]
  inputs: JsonObject
  /** The inputs as of the last write, which may be newer than `inputs` (this render's). */
  getInputs?: () => JsonObject
  onInputs: (next: JsonObject) => void
  slot: UiSlot
  preview: ReactNode
  generate: ReactNode
}

function Problem({ children }: { children: ReactNode }) {
  return <p role="alert" className="px-3 py-2 text-[12px] text-warn">{children}</p>
}

function BoundParam({ element, context, values }: { element: HostElement; context: ElementContext; values: ParamValues }) {
  const binding = bindingOf(element, context)
  if (typeof binding === 'string') return <Problem>{binding}</Problem>
  const { name, bind, param, value, mistyped } = binding
  // `data-param`, as ParameterPanel's rows carry it: the agent's highlight (#254) finds
  // the parameter it just changed by it (`findParamRow`).
  return (
    <div data-param={name}>
      {mistyped && <Problem>{mistyped}</Problem>}
      <ParamWidget
        param={param}
        value={value}
        slug={context.slug}
        version={context.version}
        fonts={context.fonts}
        extruder={extrudersOf(context.schema, values).get(name)}
        onChange={(next) => context.onInputs(setPath(context.getInputs?.() ?? context.inputs, bind, next))}
      />
    </div>
  )
}

export function HostElementContent({
  element,
  context,
  values,
  firstPreview,
}: {
  element: HostElement
  context: ElementContext
  /** The rendered values (`inputs.params`), for the extruder numbers. */
  values: ParamValues
  /** The one `<sb-preview>` the preview mounts into: one canvas, one capture ref. */
  firstPreview: HostElement | undefined
}) {
  switch (element.localName) {
    case 'sb-param':
      return <BoundParam element={element} context={context} values={values} />
    case 'sb-preview':
      if (context.slot !== 'page') {
        return <Problem>The preview is beside the panel in the panel slot; &lt;sb-preview&gt; shows it only in the page slot.</Problem>
      }
      return element === firstPreview ? context.preview : <Problem>Only the first &lt;sb-preview&gt; shows the preview.</Problem>
    case 'sb-generate':
      return context.generate
    default:
      return null
  }
}
