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
  const [search, setSearch] = useSearchParams()
  const reopenId = search.get('from')
  // #90 — "Customize this version": render an old revision without restoring it.
  const version = search.get('version') ?? undefined

  const schemaState = useAsync(() => api.getSchema(slug, version), [slug, version])
  const fontsState = useAsync(() => api.listFonts(), [])
  const outputsState = useAsync(() => api.listOutputs(slug), [slug])

  // The values carry the revision they were derived from. `version` changes the
  // moment the URL does, but the new schema is a fetch away and the values a
  // debounce beyond that — so a bare `useRenderJob(slug, debounced, version)`
  // fires at once, pairing the NEW revision id with the PREVIOUS revision's
  // parameters. That renders the wrong thing, and 422s outright (§6.1) on a
  // parameter the old schema had and the new one does not. Tagging the values
  // is what lets the render wait for its own revision.
  const [pending, setPending] = useState<{ version: string; values: ParamValues }>({
    version: '',
    values: {},
  })
  const [saved, setSaved] = useState<{ jobId: string; output: Output } | undefined>(undefined)
  const captureRef = useRef<PreviewCapture | null>(null)

  const schema = schemaState.data
  const reopened = reopenId ? outputsState.data?.find((o) => o.id === reopenId) : undefined
  const values = pending.values

  useEffect(() => {
    if (!schema) return
    setPending({
      version: version ?? '',
      values: reopened ? { ...defaultValues(schema), ...reopened.params } : defaultValues(schema),
    })
  }, [schema, reopened, version])

  // One debounce over the pair, so the tag can never lag the values it labels.
  const debounced = useDebounced(pending, RENDER_DEBOUNCE_MS)
  const current = debounced.version === (version ?? '')
  const {
    job,
    rendering,
    error: renderError,
  } = useRenderJob(slug, current ? debounced.values : undefined, version)

  // A parameter change invalidates the saved output — Generate has to run again.
  const settled = current && debounced === pending
  const output = settled && saved && saved.jobId === job?.id ? saved.output : undefined

  const onChange = useCallback((name: string, value: ParamValue) => {
    setPending((state) => ({ ...state, values: { ...state.values, [name]: value } }))
  }, [])

  const onReset = useCallback(() => {
    if (schema) setPending((state) => ({ ...state, values: defaultValues(schema) }))
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
              reopened from {reopened.name ?? reopened.id.slice(0, 8)}
            </span>
          )}
          {version && (
            <span
              data-testid="version-badge"
              className="sb-num shrink-0 rounded-[6px] bg-accent/12 px-1.5 py-0.5 text-[11px] text-accent"
            >
              revision {version.slice(0, 7)}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {version && (
            <button
              type="button"
              onClick={() => {
                const next = new URLSearchParams(search)
                next.delete('version')
                setSearch(next, { replace: true })
              }}
              className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
            >
              Back to current
            </button>
          )}
          <Link
            to={`/m/${slug}/versions`}
            className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
          >
            Versions
          </Link>
          <Link
            to={`/m/${slug}/history`}
            className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
          >
            History
            {outputsState.data && outputsState.data.length > 0 && (
              <span className="sb-num ml-1.5 text-faint">{outputsState.data.length}</span>
            )}
          </Link>
        </div>
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
