import { useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { FoundSource, PrintSources, SourceChoice } from '../../api/types'
import { formatBytes, timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'

export const KIND_LABEL: Record<FoundSource['kind'], string> = {
  archive: "The archive's file",
  archive_source: "The archive's slicer project",
  library: 'Library file',
  printer: "On the printer's storage",
}

/** Why a mesh model cannot open a sliced file (#6916; Import print's new model says it too). */
export const SLICED_REFUSAL = 'A sliced file has no meshes to open as a model.'

/** One source as the radio group names it: its kind, and its library file or printer path. */
export function sourceKey(source: Pick<FoundSource, 'kind' | 'library_file_id' | 'path'>): string {
  return `${source.kind}:${source.library_file_id ?? source.path ?? ''}`
}

/** The source as a request names it (`SourceChoice`). */
export function sourceChoice(source: FoundSource): SourceChoice {
  return { kind: source.kind, library_file_id: source.library_file_id ?? null, path: source.path ?? null }
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const errors = (error.problem as { errors?: { msg?: string }[] }).errors
    return (error.status === 422 && errors?.[0]?.msg) || error.detail
  }
  return error instanceof Error ? error.message : String(error)
}

/**
 * Where an archive's 3MF can be (`GET /print-imports/{id}/sources`). The printer's storage
 * is read only after `askPrinter`, and the last answer stays while it is read, so the list
 * never blinks.
 */
export function usePrintSources(archiveId: number) {
  const [printer, setPrinter] = useState(false)
  const [asks, setAsks] = useState(0)
  const found = useAsync(() => api.getPrintSources(archiveId, { printer }), [archiveId, printer, asks])
  const [last, setLast] = useState(found.data)
  if (found.data && found.data !== last) setLast(found.data)
  return {
    found,
    sources: found.data ?? last,
    printer,
    askPrinter: () => (printer ? setAsks((n) => n + 1) : setPrinter(true)),
  }
}

export type PrintSourcesState = ReturnType<typeof usePrintSources>

/**
 * The one picker of where a print's 3MF is (#6916): Import print (#4698) and "Open a mesh
 * as a model" both use it, so a source added here shows in both. A source that cannot be
 * used says why; with `meshOnly`, a sliced one cannot be used either.
 */
export function PrintSourceChoice({
  state,
  sources,
  choice,
  onChoose,
  disabled,
  meshOnly = false,
}: {
  state: PrintSourcesState
  sources: PrintSources
  choice: string | null
  onChoose: (key: string) => void
  disabled?: boolean
  meshOnly?: boolean
}) {
  const { found, printer, askPrinter } = state
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium">Where is its 3MF?</legend>
      <RadioGroup aria-label="Source" value={choice ?? ''} onValueChange={onChoose} className="gap-1">
        {sources.sources.map((s) => {
          const key = sourceKey(s)
          const usable = usableSource(s, meshOnly)
          const reason = !s.available ? s.reason : usable ? null : SLICED_REFUSAL
          const facts = [
            s.name ?? '',
            typeof s.size === 'number' ? formatBytes(s.size) : '',
            s.modified_at ? timeAgo(s.modified_at) : '',
            s.sliced ? 'sliced' : '',
            s.matched_by === 'hash' ? 'same file' : s.matched_by === 'name' ? 'same name' : '',
          ].filter(Boolean)
          return (
            <label key={key} className={`flex items-start gap-2 text-sm ${usable ? '' : 'opacity-60'}`} data-testid={`source-${key}`}>
              <RadioGroupItem value={key} disabled={!usable || disabled} aria-label={`${KIND_LABEL[s.kind]} ${s.name ?? ''}`.trim()} className="mt-0.5" />
              <span className="flex min-w-0 flex-col">
                <span>{KIND_LABEL[s.kind]}</span>
                {facts.length > 0 && <span className="break-all text-xs text-muted-foreground">{facts.join(' · ')}</span>}
                {reason && <span className="text-xs text-warn">{reason}</span>}
              </span>
            </label>
          )
        })}
      </RadioGroup>
      {(!printer || (found.error && !found.loading)) && (
        <div className="flex flex-col items-start gap-1">
          <Button type="button" variant="outline" size="sm" onClick={askPrinter} disabled={disabled}>
            {printer ? 'Ask the printer again' : 'Also look on the printer'}
          </Button>
          <span className="text-xs text-muted-foreground">
            {meshOnly
              ? "Lists the printer's storage. Its file is read once to make the model."
              : "Lists the printer's storage. Its file is copied into Bambuddy's library if you import from it."}
          </span>
        </div>
      )}
      {printer && found.loading && <p className="text-xs text-muted-foreground">Asking the printer…</p>}
      {printer && found.error && <p className="text-xs text-warn">Could not ask the printer: {errorText(found.error)}</p>}
    </fieldset>
  )
}

/** Whether the picker lets the person choose ``source``. */
export function usableSource(source: FoundSource, meshOnly: boolean): boolean {
  return source.available && !(meshOnly && source.sliced)
}
