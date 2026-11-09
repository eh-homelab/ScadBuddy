import { useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { CustomizerSchema, ParamValue } from '../../api/types'
import { defaultValues, type ParamValues } from '../../lib/params'
import { inputsToSend } from './printSetups'
import { useAsync } from '../../lib/useAsync'
import { ParameterPanel } from '../ParameterPanel'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'

/** The item's design a slice starts from: its template, commit and inputs (`OrderItem.reference`). */
export interface ItemDesign {
  slug: string
  commit: string
  inputs: Record<string, unknown>
}

/** Why a template with its own page takes no inputs here (#4673 PR 4). */
export const OWN_UI_TEXT = 'This template has its own page: change its inputs there and add the output to the order.'

/** The item's own parameter values: `inputs.params`, the ones a template's form edits. */
function paramsOf(inputs: Record<string, unknown>): ParamValues {
  const params = inputs['params']
  return params && typeof params === 'object' && !Array.isArray(params) ? (params as ParamValues) : {}
}

/**
 * #4673 PR 4 (order operations spec §5.2) — the slice's inputs, editable in place: the
 * template's form at the item's commit, opened on the item's values. `onChange` gets the
 * inputs to send, the item's with the edited parameters over them, or null while nothing
 * differs from the item's (a slice of the same design makes no revision). A template that
 * owns its page (`ui`) is not edited here.
 */
export function DesignInputs({ design, onChange }: { design: ItemDesign; onChange: (inputs: Record<string, unknown> | null) => void }) {
  const schema = useAsync(() => api.getSchema(design.slug, design.commit), [design.slug, design.commit])
  const fonts = useAsync(() => api.listFonts(), [])
  const own = useMemo(() => paramsOf(design.inputs), [design.inputs])
  const [edits, setEdits] = useState<ParamValues>({})
  const form = schema.data as (CustomizerSchema & { ui?: unknown }) | undefined
  const defaults = useMemo(() => (form ? defaultValues(form) : {}), [form])
  const changed = inputsToSend(design.inputs, defaults, own, edits) !== null
  const values = useMemo(() => ({ ...defaults, ...own, ...edits }), [defaults, own, edits])

  function publish(next: ParamValues) {
    setEdits(next)
    onChange(inputsToSend(design.inputs, defaults, own, next))
  }

  return (
    <Collapsible className="mb-4 rounded-md border px-3 py-2" data-testid="design-inputs">
      <CollapsibleTrigger className="cursor-pointer text-left text-sm">
        Inputs{changed ? ' (changed: a new revision)' : ''}
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">
          New inputs make a new revision of the item first; the quote renders and slices it.
        </p>
        {schema.error ? (
          <p role="alert" className="text-xs text-warn">
            The template&rsquo;s parameters could not be read: {String(schema.error)}
          </p>
        ) : !form ? (
          <Skeleton className="h-16 w-full" />
        ) : form.ui ? (
          <p className="text-xs text-muted-foreground">{OWN_UI_TEXT}</p>
        ) : (
          <ParameterPanel
            schema={form}
            slug={design.slug}
            version={design.commit}
            values={values}
            fonts={fonts.data ?? []}
            onChange={(name: string, value: ParamValue) => publish({ ...edits, [name]: value })}
            onReset={() => publish({})}
          />
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
