import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { Output, ParamValue } from '../api/types'
import { ActionBar } from '../components/ActionBar'
import { ParameterPanel } from '../components/ParameterPanel'
import type { PreviewCapture } from '../components/Preview'

// three.js is a third of the bundle and only the customizer needs it.
const Preview = lazy(async () => ({ default: (await import('../components/Preview')).Preview }))
import { Spinner } from '../components/ui/Spinner'
import { defaultValues, type ParamValues } from '../lib/params'
import { useAsync } from '../lib/useAsync'
import { useDebounced } from '../lib/useDebounced'
import { RENDER_DEBOUNCE_MS, useRenderJob } from '../lib/useRenderJob'

export function CustomizePage() {
  const { slug = '' } = useParams()
  const [search] = useSearchParams()
  const reopenId = search.get('from')

  const schemaState = useAsync(() => api.getSchema(slug), [slug])
  const fontsState = useAsync(() => api.listFonts(), [])
  const outputsState = useAsync(() => api.listOutputs(slug), [slug])
  // Resolved through /edit, not the history list: that route falls back to the 3MF's
  // own provenance when the output record is gone.
  const reopenState = useAsync(
    async () => (reopenId ? await api.getEditTarget(reopenId) : null),
    [reopenId],
  )

  const [values, setValues] = useState<ParamValues>({})
  const [saved, setSaved] = useState<{ jobId: string; output: Output } | undefined>(undefined)
  const captureRef = useRef<PreviewCapture | null>(null)

  const schema = schemaState.data
  const reopened = reopenState.data ?? undefined

  useEffect(() => {
    // Wait for the reopened values rather than rendering the defaults first.
    if (!schema || reopenState.loading) return
    setValues(reopened ? { ...defaultValues(schema), ...reopened.params } : defaultValues(schema))
  }, [schema, reopened, reopenState.loading])

  const debounced = useDebounced(values, RENDER_DEBOUNCE_MS)
  const { job, rendering, error: renderError } = useRenderJob(slug, debounced)

  // A parameter change invalidates the saved output — Generate has to run again.
  const settled = debounced === values
  const output = settled && saved && saved.jobId === job?.id ? saved.output : undefined

  const onChange = useCallback((name: string, value: ParamValue) => {
    setValues((current) => ({ ...current, [name]: value }))
  }, [])

  const onReset = useCallback(() => {
    if (schema) setValues(defaultValues(schema))
  }, [schema])

  const capture = useCallback(async () => captureRef.current?.capturePng() ?? null, [])

  if (schemaState.loading) {
    return (
      <p className="flex h-full items-center justify-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading model
      </p>
    )
  }

  if (schemaState.error || !schema) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">That model is not here</h1>
        <p className="mt-2 text-[13px] text-muted">
          {schemaState.error?.message ?? 'The model has no customizer schema.'}
        </p>
        <Link to="/" className="mt-4 inline-block text-[13px] text-accent underline">
          Back to models
        </Link>
      </div>
    )
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)]">
      <div className="flex items-center justify-between gap-3 border-b border-line bg-surface px-3 py-1.5">
        <div className="flex min-w-0 items-baseline gap-2">
          <Link to="/" className="shrink-0 text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <h1 className="truncate text-[13px] font-medium">{schema.title}</h1>
          {reopened && (
            <span className="sb-num shrink-0 text-[11px] text-faint">
              reopened from {reopened.name ?? reopened.output_id.slice(0, 8)}
            </span>
          )}
        </div>
        <Link
          to={`/m/${slug}/history`}
          className="shrink-0 rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
        >
          History
          {outputsState.data && outputsState.data.length > 0 && (
            <span className="sb-num ml-1.5 text-faint">{outputsState.data.length}</span>
          )}
        </Link>
      </div>

      <div className="grid min-h-0 grid-cols-1 lg:grid-cols-[minmax(300px,360px)_minmax(0,1fr)]">
        <div className="min-h-0 max-lg:max-h-[45vh] max-lg:border-b max-lg:border-line">
          <ParameterPanel
            schema={schema}
            values={values}
            fonts={fontsState.data ?? []}
            onChange={onChange}
            onReset={onReset}
          />
        </div>

        <div className="grid min-h-0 grid-rows-[minmax(0,1fr)_auto]">
          <Suspense
            fallback={
              <div className="flex h-full items-center justify-center bg-bg text-[13px] text-faint">
                Loading the viewer
              </div>
            }
          >
            <Preview job={job} rendering={rendering || !settled} captureRef={captureRef} />
          </Suspense>
          {renderError && (
            <p role="alert" className="border-t border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn">
              {renderError.message}
            </p>
          )}
          <ActionBar
            slug={slug}
            job={job}
            rendering={rendering || !settled}
            output={output}
            capture={capture}
            onGenerated={(created) => {
              if (job) setSaved({ jobId: job.id, output: created })
              outputsState.reload()
            }}
            onSent={() => outputsState.reload()}
            onRan={() => outputsState.reload()}
          />
        </div>
      </div>
    </div>
  )
}
