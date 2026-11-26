import { useState } from 'react'
import type { Filament } from '../../api/types'
import { Button } from '@/components/ui/button'
import { DesignDialog } from './DesignDialog'
import { sameJson, type JsonObject } from '../../lib/inputs'
import { designText, hasInputs, inputsRefusalText, pipelineOf, withDesign, withoutDesign, withoutInput } from './designInputs'
import { refusalText, shownParams, type DesignSchema, type ListingDraft, type ListingRefusal } from './listingDraft'

interface Props {
  draft: ListingDraft
  onChange: (draft: ListingDraft) => void
  /** The design's schema at the listing's version; undefined until it is read. */
  schema: DesignSchema | undefined
  /** The design's name. */
  name: string
  filaments: readonly Filament[]
  /** The listing's colours and its defaults are in, so a design set up lands on them. */
  settled: boolean
  refusal: ListingRefusal | null
}

/**
 * #5701 (spec 2026-11-17 §5.1) — what a pipeline template's listing sells, in words, under
 * the Design line: the template's own description of it, else its inputs' titles and values;
 * the button that sets it up; and what publish refused about it. Nothing for a template with
 * no pipeline.
 */
export function DesignBlock({ draft, onChange, schema, name, filaments, settled, refusal }: Props) {
  const [setting, setSetting] = useState<string | null>(null)
  /** What the template said of the inputs it set, for as long as they are the listing's. */
  const [said, setSaid] = useState<{ inputs: ListingDraft['inputs']; commit: string; text: string } | null>(null)
  const pipeline = pipelineOf(schema)
  const refusedInputs = refusal?.kind === 'inputs' ? refusal : null
  const refusedParam = refusal?.kind === 'param' && refusal.code === 'pipeline_set' && schema && !shownParams(schema).some((param) => param.name === refusal.name) ? refusal : null
  const stray = schema !== undefined && !pipeline && hasInputs(draft)
  if (!pipeline && !stray && !refusedInputs && !refusedParam) return null

  // One opening per design and version: another one picked while it is open closes it.
  const key = `${draft.slug}\n${draft.commit}`
  const text =
    said && sameJson(said.inputs as JsonObject, (draft.inputs ?? {}) as JsonObject) && said.commit === draft.commit
      ? said.text
      : hasInputs(draft)
        ? designText(pipeline?.inputs, draft.inputs ?? {})
        : draft.inputs_v != null
          ? 'The design the template starts with'
          : 'Not set up yet'
  return (
    <div role="group" aria-label="Design" className="flex flex-col gap-1.5 rounded-md border p-3" data-testid="listing-design-inputs">
      {pipeline && (
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="flex min-w-0 flex-1 basis-60 flex-col gap-0.5">
            <p className="text-sm font-medium break-words">{text}</p>
            <p className="text-xs text-muted-foreground">Set by you. Customers cannot change it.</p>
          </div>
          {pipeline.inputs && (
            <Button variant="outline" size="sm" disabled={!settled} onClick={() => setSetting(key)}>
              Set up the design
            </Button>
          )}
        </div>
      )}
      {pipeline && !pipeline.inputs && !refusedInputs && (
        <p className="text-xs text-warn">This version of the template cannot be listed: {pipeline.fault}. Choose another version.</p>
      )}
      {stray && !refusedInputs && <p className="text-xs text-warn">This version of the template has no design to set up. Clear the design, or choose another version.</p>}
      {refusedInputs && (
        <p role="alert" className="text-xs break-words text-destructive">
          {inputsRefusalText(refusedInputs, pipeline?.inputs)}
        </p>
      )}
      {refusedParam && (
        <p role="alert" className="text-xs break-words text-destructive">
          {refusalText(refusedParam, schema?.parameters?.find((param) => param.name === refusedParam.name)?.caption || refusedParam.name)}
        </p>
      )}
      {stray && (
        <Button variant="outline" size="sm" className="self-start" onClick={() => onChange(withoutDesign(draft))}>
          Clear the design
        </Button>
      )}
      {pipeline && refusedInputs?.reason === 'additional' && (
        <Button variant="outline" size="sm" className="self-start" onClick={() => onChange(withoutInput(draft, refusedInputs.name))}>
          Remove it
        </Button>
      )}
      {setting === key && schema && pipeline && (
        <DesignDialog
          draft={draft}
          schema={schema}
          name={name}
          onClose={() => setSetting(null)}
          onUse={(inputs, words) => {
            const next = withDesign(draft, schema, filaments, inputs)
            setSaid(words === null ? null : { inputs: next.inputs, commit: next.commit, text: words })
            setSetting(null)
            onChange(next)
          }}
        />
      )}
    </div>
  )
}
