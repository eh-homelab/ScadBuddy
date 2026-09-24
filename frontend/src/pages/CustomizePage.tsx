import { Suspense, lazy, useCallback, useMemo, useRef, useState } from 'react'
import { Link, Navigate, useLocation, useParams, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { Output, ParamValue } from '../api/types'
import { ActionBar } from '../components/ActionBar'
import { ParameterPanel } from '../components/ParameterPanel'
import type { PreviewCapture } from '../components/Preview'

// three.js is a third of the bundle and only the customizer needs it.
const Preview = lazy(async () => ({ default: (await import('../components/Preview')).Preview }))
import { Spinner } from '../components/ui/Spinner'
import { editPath, type EditNavigationState } from '../lib/deeplink'
import { defaultValues, type ParamValues } from '../lib/params'
import { useAsync } from '../lib/useAsync'
import { useDebounced } from '../lib/useDebounced'
import { RENDER_DEBOUNCE_MS, useRenderJob } from '../lib/useRenderJob'

/** One shared empty map, so "nothing yet" keeps a stable identity across renders. */
const NOTHING: ParamValues = Object.freeze({})

export function CustomizePage() {
  const { slug = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const reopenId = search.get('from')
  // #90 — "Customize this version": render an old revision without restoring it.
  const version = search.get('version') ?? undefined

  const schemaState = useAsync(() => api.getSchema(slug, version), [slug, version])
  const fontsState = useAsync(() => api.listFonts(), [])
  const outputsState = useAsync(() => api.listOutputs(slug), [slug])
  // Resolved through /edit, not the history list: that route falls back to the 3MF's
  // own provenance when the output record is gone. EditPage has usually resolved it
  // already and passed it in state, so arriving that way costs no second request.
  const handedOver = useLocation().state as EditNavigationState | null
  const preloaded = handedOver?.editTarget?.output_id === reopenId ? handedOver.editTarget : null
  const reopenState = useAsync(
    async () => (reopenId && !preloaded ? await api.getEditTarget(reopenId) : null),
    [reopenId, preloaded !== null],
  )

  // `edited` is what the user has changed; `seed` is what the page opened on. Keeping
  // them apart is what lets the first paint already carry the right values — seeding
  // through an effect runs after that paint, which is one frame of the wrong numbers.
  const [edits, setEdits] = useState<{ of: ParamValues | null; values: ParamValues | null }>({
    of: null,
    values: null,
  })
  const [saved, setSaved] = useState<{ jobId: string; output: Output } | undefined>(undefined)
  const captureRef = useRef<PreviewCapture | null>(null)

  const schema = schemaState.data
  const resolved = preloaded ?? reopenState.data ?? undefined
  // An output belongs to one model. EditPage builds the URL from the resolved slug,
  // so only a typed or bookmarked link can pair an id with the wrong model — and
  // spreading another model's values onto this schema is a silent wrong answer.
  const foreign = resolved && resolved.slug !== slug ? resolved : undefined
  const reopened = foreign ? undefined : resolved
  // Every hook below runs before the redirects further down, so a page this one is
  // only passing through must not seed any values: with none there is nothing to
  // debounce, and no render — an OpenSCAD process and a concurrency slot — is started
  // for a model the reader is not going to see.
  const leaving = foreign !== undefined || Boolean(reopenId && reopenState.error)

  // useAsync reports loading whether or not it has anything to fetch, so reading it
  // directly would hold the values back for a render even when the target is already
  // in hand — long enough to paint the schema defaults and snap off them.
  const resolving = Boolean(reopenId) && !preloaded && reopenState.loading

  const seed = useMemo(
    // Null until there is something to show: the schema has to be here, and a deep
    // link's values have to have arrived, before the defaults are the right answer.
    () =>
      schema && !resolving && !leaving
        ? reopened
          ? { ...defaultValues(schema), ...reopened.params }
          : defaultValues(schema)
        : null,
    [schema, reopened, resolving, leaving],
  )
  // A different model, or a different output, discards edits made against the old one.
  if (edits.of !== seed) setEdits({ of: seed, values: null })
  const values = edits.values ?? seed ?? NOTHING

  // One debounce over the pair, so the tag can never lag the values it labels.
  const debounced = useDebounced(values, RENDER_DEBOUNCE_MS)
  // True once the debounce has caught up with the values on screen. #90: the render
  // waits for it, because `version` flips the instant the URL does while the
  // matching schema — and so the values seeded from it — are a fetch behind. Passing
  // `debounced` unconditionally pairs the NEW revision id with the PREVIOUS
  // revision's parameters for one submission: the wrong render at best, and a 422
  // (§6.1) on a parameter the old schema had and the new one does not.
  const settled = debounced === values
  const {
    job,
    rendering,
    error: renderError,
  } = useRenderJob(slug, settled ? debounced : undefined, version)

  // A parameter change invalidates the saved output — Generate has to run again.
  const output = settled && saved && saved.jobId === job?.id ? saved.output : undefined

  const onChange = useCallback((name: string, value: ParamValue) => {
    setEdits((current) => ({
      of: current.of,
      values: { ...(current.values ?? current.of ?? NOTHING), [name]: value },
    }))
  }, [])

  const onReset = useCallback(() => {
    if (schema) setEdits((current) => ({ of: current.of, values: defaultValues(schema) }))
  }, [schema])

  const capture = useCallback(async () => captureRef.current?.capturePng() ?? null, [])

  if (reopenId && reopenState.error) {
    // The deep link is dead — no record and no 3MF to read it from. /edit/{id} owns
    // that message; sending the reader there keeps one copy of it.
    return <Navigate to={editPath(reopenId)} replace />
  }

  if (foreign) {
    return (
      <Navigate
        to={`/m/${foreign.slug}?from=${foreign.output_id}`}
        state={{ editTarget: foreign } satisfies EditNavigationState}
        replace
      />
    )
  }

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
