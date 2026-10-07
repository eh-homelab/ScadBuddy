import { Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, useLocation, useParams, useSearchParams } from 'react-router'
import { committed, touchAfterRender, waitFor } from '../agent/highlight'
import { AgentToolError } from '../agent/types'
import { useAgentHandlers, useLatest } from '../agent/useAgentHandlers'
import { api } from '../api/client'
import type { Output, Param, ParamValue, Plate } from '../api/types'
import { ResourceSessions } from '../components/assistant/ResourceSessions'
import { ActionBar, type ActionBarHandle } from '../components/ActionBar'
import { DeleteModelButton } from '../components/DeleteModelButton'
import { DuplicatedFrom, DuplicateModelButton } from '../components/DuplicateModelButton'
import { EditDetailsButton } from '../components/EditDetailsButton'
import { MediaButton } from '../components/media/MediaButton'
import { FlyoutHeader, FullscreenButton, ParametersButton } from '../components/FullscreenControls'
import { ModelLibrariesButton } from '../components/ModelLibrariesButton'
import { PreviewGallery } from '../components/media/PreviewGallery'
import { ParameterPanel } from '../components/ParameterPanel'
import { RawInputs } from '../components/RawInputs'
import { PresetPicker } from '../components/PresetPicker'
import type { PreviewCapture } from '../components/Preview'
import { Button } from '../components/ui/Button'
import { UpstreamUpdateButton } from '../components/UpstreamUpdate'

// three.js is a third of the bundle and only the customizer needs it.
const Preview = lazy(async () => ({ default: (await import('../components/Preview')).Preview }))
import { Spinner } from '../components/ui/Spinner'
import { editPath, modelPath, type EditNavigationState } from '../lib/deeplink'
import {
  allParams,
  checkParamValue,
  defaultValues,
  diffFromDefaults,
  outOfRange,
  rangeProblem,
  sameValues,
  type ParamValues,
} from '../lib/params'
import {
  isJsonObject,
  joinInputs,
  NO_EXTRA,
  sameJson,
  splitInputs,
  type InputsExtra,
  type JsonObject,
} from '../lib/inputs'
import { migrateIfOld, type MigrateOutcome } from '../lib/useMigratedInputs'
import { applyPreset, presetInputs } from '../lib/presets'
import { saveOutput } from '../lib/saveOutput'
import { findParamRow } from '../template-ui/elements'
import type { HostDeps } from '../template-ui/host'
import { TemplateUi } from '../template-ui/TemplateUi'
import type { TemplateUiFailure, UiDeclaration } from '../template-ui/types'
import { fitTargets, platesFitMessages, worstFit } from '../lib/plate'
import type { SnapshotOptions } from '../lib/snapshot'
import { useDisplayUnit } from '../lib/units'
import { useSubscription } from '../lib/realtime'
import { useAsync } from '../lib/useAsync'
import { useDebounced } from '../lib/useDebounced'
import { useFullscreen } from '../lib/useFullscreen'
import { RENDER_DEBOUNCE_MS, type RenderBusy, useRenderJob, canRetry } from '../lib/useRenderJob'

/** One shared empty map, so "nothing yet" keeps a stable identity across renders. */
const NOTHING: ParamValues = Object.freeze({})

const FLYOUT_ID = 'parameters-flyout'
/**
 * The flyout's width, which the readouts move clear of: set on the full-screen workspace
 * per breakpoint (`--sb-flyout`), a sheet over the whole view below `md` and the docked
 * column's widest, 360px, from there up.
 */
const FLYOUT_WIDTH = 'var(--sb-flyout)'

/** The banner while a refused render waits to be sent again, worded by why it waits. */
function renderBusyText({ seconds, reason }: RenderBusy): string {
  switch (reason) {
    case 'temporal-unavailable':
      return `ScadBuddy cannot reach its render service; retrying in ${seconds} s.`
    case 'still-accepting':
      return `The render service is still accepting this preview; checking again in ${seconds} s.`
    case 'unanswered':
      return `ScadBuddy did not answer; this preview will be retried in ${seconds} s.`
    case 'queue-full':
      return `The render queue is full; this preview will be retried in ${seconds} s.`
  }
}

/** An import's origin for the page's label; a URL the record holds that does not parse
 *  must not take the page down. */
function importedFrom(originUrl: string): string {
  try {
    return `imported from ${new URL(originUrl).host}`
  } catch {
    return 'imported'
  }
}

/** ``current`` with ``changed`` over its parameters. */
function withParams(current: JsonObject, changed: ParamValues): JsonObject {
  const { params, extra } = splitInputs(current)
  return joinInputs({ ...params, ...changed }, extra)
}

export function CustomizePage() {
  const { slug = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const reopenId = search.get('from')
  // #90 — "Customize this version": render an old revision without restoring it.
  const version = search.get('version') ?? undefined

  const schemaState = useAsync(() => api.getSchema(slug, version), [slug, version])
  const fontsState = useAsync(() => api.listFonts(), [])
  // #269 — live: an output saved from another tab, or by an agent, appears here.
  const outputsState = useAsync(() => api.listOutputs(slug), [slug], [`model:${slug}`])
  // #184 — a built-in is read-only on the server; the write actions only show once
  // the record says the model is the user's, so a built-in never flashes them.
  // #269 — live: details, pins and the upstream badge follow changes made elsewhere.
  const modelState = useAsync(() => api.getModel(slug), [slug], [`model:${slug}`])
  const origin = modelState.data?.origin
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
  // `extra` is a template UI's state (spec 2026-09-27 §4.3), kept beside the values.
  const [edits, setEdits] = useState<{
    of: ParamValues | null
    values: ParamValues | null
    extra: InputsExtra | null
  }>({ of: null, values: null, extra: null })
  const [saved, setSaved] = useState<{ jobId: string; extra: InputsExtra; output: Output } | undefined>(undefined)
  // The template's own Generate (<sb-generate>): its save in flight, and why the last failed.
  const [uiGenerate, setUiGenerate] = useState<{ generating: boolean; error: string | null }>({
    generating: false,
    error: null,
  })
  const captureRef = useRef<PreviewCapture | null>(null)

  /**
   * #81 — the model of the printer chosen in the print picker, which picks the plate the
   * preview draws and the model is checked against. `null` until one is chosen, and the
   * server then answers with the configured default plate.
   */
  const [printerModel, setPrinterModel] = useState<string | null>(null)
  const plateState = useAsync(() => api.getPlate(printerModel), [printerModel])
  // The previous plate stays up while the next one loads, rather than blinking out.
  const [plate, setPlate] = useState<Plate | undefined>(undefined)
  if (plateState.data && plateState.data !== plate) setPlate(plateState.data)

  const schema = schemaState.data
  const resolved = preloaded ?? reopenState.data ?? undefined
  // An output belongs to one model. EditPage builds the URL from the resolved slug,
  // so only a typed or bookmarked link can pair an id with the wrong model — and
  // spreading another model's values onto this schema is a silent wrong answer.
  const foreign = resolved && resolved.slug !== slug ? resolved : undefined
  const reopened = foreign ? undefined : resolved
  // spec 2026-09-27 §7: an arranged output has no one set of inputs to reopen; /edit/{id}
  // says so, as it does for a dead link.
  const arranged = (reopened?.arranged_from ?? []).length > 0
  // Every hook below runs before the redirects further down, so a page this one is
  // only passing through must not seed any values: with none there is nothing to
  // debounce, and no render — an OpenSCAD process and a concurrency slot — is started
  // for a model the reader is not going to see.
  const leaving = foreign !== undefined || arranged || Boolean(reopenId && reopenState.error)

  // useAsync reports loading whether or not it has anything to fetch, so reading it
  // directly would hold the values back for a render even when the target is already
  // in hand — long enough to paint the schema defaults and snap off them.
  // An output's inputs are brought up to the template's INPUTS_VERSION before they are
  // applied (spec 2026-09-27 §8.2): the version is on the model record.
  const inputsVersion = modelState.data?.inputs_version
  const reopenedRaw = useMemo<JsonObject | null>(
    () =>
      reopened
        ? isJsonObject(reopened.inputs)
          ? (reopened.inputs as JsonObject)
          : joinInputs(reopened.params ?? {}, NO_EXTRA)
        : null,
    [reopened],
  )
  const migration = useAsync(
    async () =>
      reopenedRaw && inputsVersion !== undefined
        ? await migrateIfOld(slug, reopenedRaw, inputsVersion, version)
        : null,
    [slug, reopenedRaw, inputsVersion ?? null, version ?? null],
  )
  // Held back until the model says which version is current (a model that did not load
  // applies the inputs as they are), then until the migration has answered.
  const migrating =
    reopenedRaw !== null &&
    ((inputsVersion === undefined && !modelState.error) ||
      (inputsVersion !== undefined && migration.loading))
  const reopenOutcome: MigrateOutcome | null = migration.data ?? null

  const resolving = (Boolean(reopenId) && !preloaded && reopenState.loading) || migrating

  const reopenedInputs = useMemo(() => {
    if (!reopened) return null
    if (reopenOutcome?.kind === 'failed') return null // the current (default) state stays
    const raw = reopenOutcome?.kind === 'ready' ? reopenOutcome.inputs : reopened.inputs
    return splitInputs(raw, reopened.params)
  }, [reopened, reopenOutcome])
  // Inputs that could not be migrated, shown read-only until something else is loaded.
  const [presetFailure, setPresetFailure] = useState<{ inputs: JsonObject; error: string } | null>(null)
  const [dismissedReopen, setDismissedReopen] = useState<MigrateOutcome | null>(null)
  const unmigrated =
    presetFailure ??
    (reopenOutcome?.kind === 'failed' && reopenOutcome !== dismissedReopen ? reopenOutcome : null)
  const seed = useMemo(
    // Null until there is something to show: the schema has to be here, and a deep
    // link's values have to have arrived, before the defaults are the right answer.
    () =>
      schema && !resolving && !leaving
        ? reopenedInputs
          ? { ...defaultValues(schema), ...reopenedInputs.params }
          : defaultValues(schema)
        : null,
    [schema, reopenedInputs, resolving, leaving],
  )
  // A different model, or a different output, discards edits made against the old one.
  if (edits.of !== seed) setEdits({ of: seed, values: null, extra: null })
  const values = edits.values ?? seed ?? NOTHING
  const extra = edits.extra ?? reopenedInputs?.extra ?? NO_EXTRA

  // #425 — a template's own interface (spec 2026-09-27 §4). Whether there is one is the
  // `ui` declaration, never `extra` being non-empty: the record's for the live template,
  // the revision's own (on its schema) for an old one, which may declare another or none.
  const record = modelState.data
  const pinned = version !== undefined && schema && 'ui' in schema ? schema : undefined
  const declaration = version === undefined ? record : pinned
  const declared = (declaration?.ui ?? null) as UiDeclaration | null
  // Keyed by the revision as well: another revision's interface may work.
  const [uiFailure, setUiFailure] = useState<{
    slug: string
    version: string | undefined
    failure: TemplateUiFailure
  } | null>(null)
  const failure =
    uiFailure?.slug === slug && uiFailure.version === version
      ? uiFailure.failure
      : declaration?.ui_error
        ? { file: 'model.json', message: declaration.ui_error }
        : null
  const customUi = declared && !failure ? declared : null
  // A failure is not for good: a reload of the record or the schema, or the banner's own
  // button, tries the interface again.
  const reloadModel = () => {
    setUiFailure(null)
    modelState.reload()
  }
  const retryUi = () => {
    if (uiFailure?.slug === slug && uiFailure.version === version) setUiFailure(null)
    else reloadModel() // a declaration model.json could not hold: read the record again
  }
  // `host.openPrint` misuse, reported as the interface's failure rather than thrown at it.
  const reportUi = useLatest((message: string) =>
    setUiFailure({ slug, version, failure: { file: declared?.module ?? 'ui', message } }),
  )
  // Wait for the record and the schema before choosing, so a template with a UI never
  // flashes the form, and a UI never mounts before `host.schema()` can answer.
  const choosing = (!record && !modelState.error) || !schema
  const [presetsRevision, setPresetsRevision] = useState(0)
  /** #350 — counts resets to the defaults, which leave no preset selected. */
  const [resets, setResets] = useState(0)
  const inputs = useMemo(() => joinInputs(values, extra), [values, extra])
  // The inputs as of the last write, ahead of the render that shows it: two writes in one
  // tick (`host.inputs.set`, then an `<sb-param>` edit) each start from the one before.
  const latestInputs = useRef(inputs)
  useLayoutEffect(() => {
    latestInputs.current = inputs
  }, [inputs])
  // Every writer advances it at once, before its `setEdits` renders: a template UI that
  // awaits a preset load (or anything else) and then writes starts from what it loaded.
  const actions = useRef<ActionBarHandle>(null)
  const describeRef = useRef<(() => string) | null>(null)
  // The interface follows the last commit to its `ui/` (#846), not the record's: a commit
  // to the details, README or media must not tear a mounted interface down.
  const uiVersion = version ?? record?.ui_version ?? record?.version ?? undefined

  // #269 — the source changed elsewhere (another tab, an agent). With no edits the
  // parameters follow it at once; with edits they are the user's, so the page asks.
  // An old revision (`version`) never changes, so it has nothing to follow.
  // Held as the slug it is about, so it never follows the user to another model
  // (Duplicate navigates here without remounting the page).
  const [sourceChangedFor, setSourceChangedFor] = useState<string | null>(null)
  const sourceChanged = sourceChangedFor === slug
  const dirty = edits.values !== null
  // Read when the answer lands: the user may have started editing while it was read.
  const isDirty = useLatest(() => dirty)
  useSubscription(version === undefined ? `model:${slug}` : undefined, (signal) => {
    if (signal === 'resync' || signal.kind !== 'source.changed') return
    if (isDirty.current()) {
      setSourceChangedFor(slug)
      return
    }
    schemaState.refresh(() => {
      if (!isDirty.current()) return true
      setSourceChangedFor(slug)
      return false
    })
  })
  const reloadSchema = () => {
    setUiFailure(null) // the source may now hold a working interface
    setSourceChangedFor(null)
    setEdits({ of: seed, values: null, extra: null })
    schemaState.refresh()
  }

  // One debounce over the pair, so the tag can never lag the values it labels.
  const debounced = useDebounced(values, RENDER_DEBOUNCE_MS)
  // True once the debounce has caught up with the values on screen. #90: the render
  // waits for it, because `version` flips the instant the URL does while the
  // matching schema — and so the values seeded from it — are a fetch behind. Passing
  // `debounced` unconditionally pairs the NEW revision id with the PREVIOUS
  // revision's parameters for one submission: the wrong render at best, and a 422
  // (§6.1) on a parameter the old schema had and the new one does not.
  const settled = debounced === values
  // #921 — a number outside its declared range is flagged on its field; the render
  // would only answer 422, so none is started and Generate waits until it is fixed.
  const unrenderable = schema ? outOfRange(schema, values) : undefined
  const invalid = unrenderable ? rangeProblem(unrenderable, values[unrenderable.name]) : null
  // Nothing to render until there is a seed; once there is, an empty one is a model
  // with no parameters, whose defaults still render (#941).
  const {
    job,
    rendering,
    error: renderError,
    busy: renderBusy,
    retry: retryRender,
    settledFor,
    stage: renderStage,
  } = useRenderJob(slug, settled && seed && !invalid ? debounced : undefined, version, extra)
  // #938 — the colours the latest finished render used and the values it ran with, kept
  // while the next one runs so the extruder labels do not fall back to a guess and back
  // on every change. Kept per model, so another model's render never labels this one's.
  const [rendered, setRendered] = useState<
    { slug: string; colors: string[]; params: ParamValues } | undefined
  >(undefined)
  const doneColors = job?.status === 'done' ? (job.colors ?? undefined) : undefined
  const doneParams = settledFor ?? NOTHING
  if (doneColors && (doneColors !== rendered?.colors || doneParams !== rendered.params))
    setRendered({ slug, colors: doneColors, params: doneParams })
  const renderedOutput = rendered?.slug === slug ? rendered : undefined
  // The job on screen is the render of the values on screen — not the previous one,
  // which is all `settled && !rendering` can promise for a frame after a change.
  const upToDate = settled && settledFor === debounced && !rendering && !invalid

  // A parameter change invalidates the saved output — Generate has to run again. So does
  // a UI-state-only change (#848): it starts no render, but the output records the old state.
  const output =
    settled && !invalid && saved && saved.jobId === job?.id && sameJson(saved.extra, extra) ? saved.output : undefined

  // #289 — a multi-plate render is checked plate by plate.
  const targets = useMemo(() => fitTargets(job), [job])
  const fitState = useAsync(
    async () =>
      targets.length > 0
        ? await Promise.all(targets.map((target) => api.getPlateFit(printerModel, target.size, target.colours)))
        : null,
    // useAsync keys by the deps' JSON, so a re-render with equal targets fetches nothing.
    [printerModel, targets],
  )
  const fits = fitState.data ?? []
  const fit = worstFit(fits)
  const unit = useDisplayUnit()
  const misfit = platesFitMessages(fits, targets, unit)

  const onChange = useCallback((name: string, value: ParamValue) => {
    latestInputs.current = withParams(latestInputs.current, { [name]: value })
    setEdits((current) => ({
      of: current.of,
      values: { ...(current.values ?? current.of ?? NOTHING), [name]: value },
      extra: current.extra,
    }))
  }, [])

  const onReset = useCallback(() => {
    if (schema) {
      setPresetFailure(null)
      setDismissedReopen(reopenOutcome)
      latestInputs.current = joinInputs(defaultValues(schema), NO_EXTRA)
      setEdits((current) => ({ of: current.of, values: defaultValues(schema), extra: NO_EXTRA }))
      setResets((n) => n + 1)
    }
  }, [schema, reopenOutcome])

  const onApplyPreset = useCallback(
    (next: ParamValues, nextExtra: InputsExtra) => {
      setPresetFailure(null)
      setDismissedReopen(reopenOutcome)
      latestInputs.current = joinInputs(next, nextExtra)
      setEdits((current) => ({ of: current.of, values: next, extra: nextExtra }))
    },
    [reopenOutcome],
  )

  // A preset's stored inputs, brought up to the template version before the picker cuts
  // them to the schema (spec §8.2). Current ones (or a version not known yet) pass as
  // they are, at once; ones that cannot be migrated show read-only instead.
  const migratePreset = useCallback(
    (saved: JsonObject): JsonObject | Promise<JsonObject | null> => {
      const v = typeof saved.v === 'number' ? saved.v : 0
      if (inputsVersion === undefined || v === inputsVersion) return saved
      return migrateIfOld(slug, saved, inputsVersion, version).then((outcome) => {
        if (outcome.kind === 'failed') {
          setPresetFailure({ inputs: outcome.inputs, error: outcome.error })
          return null
        }
        return outcome.inputs
      })
    },
    [slug, version, inputsVersion],
  )

  const capture = useCallback(async () => captureRef.current?.capturePng() ?? null, [])
  const captureImage = useCallback(
    async (options: SnapshotOptions) => captureRef.current?.captureImage(options) ?? null,
    [],
  )
  const viewSize = useCallback(
    () => captureRef.current?.viewSize() ?? { width: 0, height: 0 },
    [],
  )
  const cameraView = useCallback(() => captureRef.current?.cameraView() ?? null, [])

  // #254 — the parameter the agent last touched: the panel shows its tab, and the row
  // gets the highlight once it is on screen.
  const [reveal, setReveal] = useState<{ name: string } | undefined>(undefined)
  const showTouched = useCallback((name: string) => {
    setReveal({ name })
    touchAfterRender(() => findParamRow(name))
  }, [])

  const live = useLatest({
    schema,
    values,
    extra,
    inputs,
    upToDate,
    ready: job?.status === 'done' && !rendering && settled && upToDate,
    job,
    output,
    reloadOutputs: outputsState.reload,
    renderError,
    printerModel,
    plateState,
    fit,
    misfit,
  })

  const schemaNow = useCallback(() => {
    const current = live.current.schema
    if (!current) throw new Error('the schema is still loading')
    return current
  }, [live])
  // The Host's Generate in flight, with the template it is for.
  const generating = useRef<{ key: string; run: Promise<{ jobId: string; outputId: string }> } | null>(null)
  const hostDeps: HostDeps = useMemo(() => {
    const key = `${slug}\n${uiVersion ?? ''}`
    return {
      slug,
      version: uiVersion,
      getSchema: schemaNow,
      getInputs: () => latestInputs.current,
      setInputs: (next: JsonObject) => {
        latestInputs.current = next
        setEdits((current) => {
          const shown = current.values ?? current.of ?? NOTHING
          const { params, extra: nextExtra } = splitInputs(next, shown)
          // UI state alone must not re-render: keep the params object's identity, which
          // the debounce and useRenderJob key on, when the params did not change.
          return { of: current.of, values: sameValues(shown, params) ? shown : params, extra: nextExtra }
        })
      },
      // One at a time at the Host, whoever calls it (<sb-generate> or the template):
      // a second call while one runs gets the same promise, so one output.
      generate: () => {
        if (generating.current?.key === key) return generating.current.run
        setUiGenerate({ generating: true, error: null })
        const run = (async () => {
          await waitFor(() => (live.current.ready ? true : undefined), {
            timeout: 120_000,
            what: 'the preview render of the current inputs',
          })
          const done = live.current.job
          if (!done) throw new Error('there is no render to keep')
          const savedExtra = live.current.extra
          const created = await saveOutput({ slug, job: done, extra: savedExtra, capture })
          setSaved({ jobId: done.id, extra: savedExtra, output: created })
          live.current.reloadOutputs()
          // Shown, or already left behind by a UI-state change made while it saved (#848).
          await committed(
            () => live.current.output?.id === created.id || !sameJson(savedExtra, live.current.extra),
            'the saved output',
          )
          return { jobId: done.id, outputId: created.id }
        })()
        generating.current = { key, run }
        run.then(
          () => setUiGenerate({ generating: false, error: null }),
          (error: unknown) =>
            setUiGenerate({ generating: false, error: error instanceof Error ? error.message : String(error) }),
        ).finally(() => {
          if (generating.current?.run === run) generating.current = null
        })
        return run
      },
      openPrint: (outputId: string) => {
        if (actions.current?.openPrint(outputId) === false) {
          reportUi.current(`openPrint(${outputId}): output is not the one on screen; call generate() first`)
        }
      },
      presets: {
        list: () => api.listPresets(slug),
        save: async (name: string) => {
          const created = await api.createPreset(slug, {
            name,
            inputs: presetInputs(schemaNow(), live.current.values, live.current.extra),
          })
          setPresetsRevision((n) => n + 1) // the picker keeps its own list; remount it
          return created
        },
        load: async (id: string) => {
          const preset = (await api.listPresets(slug)).find((candidate) => candidate.id === id)
          if (!preset) throw new Error(`no preset ${id}`)
          const applied = applyPreset(schemaNow(), preset)
          onApplyPreset(applied.values, applied.extra)
        },
      },
      onDescribe: (fn: (() => string) | null) => {
        describeRef.current = fn
      },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- everything else is read through `live`
  }, [slug, uiVersion])

  function requireSchema() {
    if (!schema) {
      throw new AgentToolError('timeout', schemaState.error ? `The model did not load: ${schemaState.error.message}` : 'The model is still loading.')
    }
    return schema
  }

  function requireParam(name: string): Param {
    const param = allParams(requireSchema()).find((entry) => entry.name === name)
    if (!param) {
      throw new AgentToolError('invalid_args', `"${slug}" has no parameter "${name}"; get_params lists them.`)
    }
    return param
  }

  function checked(name: string, value: ParamValue): ParamValue {
    const outcome = checkParamValue(requireParam(name), value)
    if (!outcome.ok) throw new AgentToolError('invalid_args', outcome.message)
    return outcome.value
  }

  function renderReport() {
    const { job: settledJob, renderError: failure, misfit: fitProblems, fit: plateFit } = live.current
    return {
      status: failure ? 'error' : settledJob?.status,
      error: failure?.message ?? settledJob?.error ?? null,
      bbox_mm: settledJob?.bbox_mm ?? null,
      colors: settledJob?.colors ?? [],
      warnings: settledJob?.warnings ?? [],
      // What the template changed from the parameters it was given (#285).
      notes: settledJob?.notes ?? [],
      // The log only earns its tokens when something went wrong.
      log_tail:
        settledJob?.status === 'failed' || settledJob?.status === 'cancelled'
          ? (settledJob.log_tail ?? []).slice(-20)
          : undefined,
      plate: plateFit?.plate.name ?? null,
      fits: fitProblems.length === 0,
      fit_problems: fitProblems,
    }
  }

  useAgentHandlers(
    'customize',
    {
      get_params: () => {
        const current = requireSchema()
        return {
          slug,
          version: version ?? null,
          params: allParams(current).map((param) => ({
            name: param.name,
            caption: param.caption ?? null,
            type: param.type,
            group: param.group || null,
            value: values[param.name] ?? param.initial ?? null,
            initial: param.initial ?? null,
            min: param.min ?? undefined,
            max: param.max ?? undefined,
            step: param.step ?? undefined,
            max_length: param.max_length ?? undefined,
            options: param.options?.map((option) => ({ name: option.name, value: option.value })),
            samples: param.samples?.length ? param.samples : undefined,
          })),
          changed: diffFromDefaults(current, values).map((diff) => diff.name),
          inputs: joinInputs(values, extra),
          ui_summary: describeRef.current?.() ?? null,
        }
      },
      set_param: async ({ name, value }) => {
        const next = checked(name, value)
        onChange(name, next)
        showTouched(name)
        await committed(() => live.current.values[name] === next)
        return { name, value: next, rendering: 'after the usual debounce; render waits for it' }
      },
      set_params: async ({ values: wanted }) => {
        const entries = Object.entries(wanted)
        if (entries.length === 0) throw new AgentToolError('invalid_args', 'values is empty.')
        const problems: string[] = []
        const next: ParamValues = {}
        for (const [name, value] of entries) {
          try {
            next[name] = checked(name, value)
          } catch (cause) {
            problems.push(cause instanceof Error ? cause.message : String(cause))
          }
        }
        // All or nothing: one bad value leaves every field as it was.
        if (problems.length > 0) throw new AgentToolError('invalid_args', problems.join(' '))
        latestInputs.current = withParams(latestInputs.current, next)
        setEdits((current) => ({
          of: current.of,
          values: { ...(current.values ?? current.of ?? NOTHING), ...next },
          extra: current.extra,
        }))
        showTouched(Object.keys(next)[0] ?? '')
        await committed(() => Object.entries(next).every(([name, value]) => live.current.values[name] === value))
        return { values: next }
      },
      reset_param: async ({ name }) => {
        if (name === undefined) {
          const current = requireSchema()
          onReset()
          await committed(() => diffFromDefaults(current, live.current.values).length === 0)
          return { reset: 'all' }
        }
        const param = requireParam(name)
        if (param.initial === null || param.initial === undefined) {
          throw new AgentToolError('invalid_args', `"${name}" has no default to go back to.`)
        }
        const initial = param.initial
        onChange(name, initial)
        showTouched(name)
        await committed(() => live.current.values[name] === initial)
        return { name, value: initial }
      },
      render: async ({ timeout_ms }) => {
        requireSchema()
        await waitFor(() => (live.current.upToDate ? true : undefined), {
          timeout: timeout_ms,
          what: 'the preview render of the current values',
        })
        return renderReport()
      },
      select_plate: async ({ printer_model }) => {
        setPrinterModel(printer_model)
        const plateNow = await waitFor(
          () => {
            const { printerModel: model, plateState: state } = live.current
            return model === printer_model && !state.loading ? state : undefined
          },
          { timeout: 10_000, what: 'the plate to load' },
        )
        if (plateNow.error) throw new AgentToolError('failed', plateNow.error.message)
        return { plate: plateNow.data ?? null }
      },
    },
    () => ({
      slug,
      version: version ?? null,
      loaded: Boolean(schema),
      changed: schema ? diffFromDefaults(schema, values).map((diff) => ({ name: diff.name, value: diff.value })) : [],
      render: upToDate ? (renderError ? 'error' : job?.status) : 'pending',
      bbox_mm: job?.status === 'done' ? job.bbox_mm : null,
      plate: plate?.name ?? null,
      fit_problems: misfit,
      saved_output: output ? { id: output.id, name: output.name ?? null } : null,
    }),
  )

  // Full screen takes the whole workspace, not the viewer alone, so the parameters can
  // come along as a flyout over the scene and a change is watched as it renders. It is
  // the same element throughout, whichever way it fills the screen: moving the canvas
  // would reload the model and lose the camera, and moving the panel would lose its tab.
  const workspace = useRef<HTMLDivElement>(null)
  const fullscreen = useFullscreen(workspace)
  const full = fullscreen.mode !== null
  const [flyout, setFlyout] = useState(false)
  // Each full screen opens on the view alone.
  if (!full && flyout) setFlyout(false)
  const flyoutButton = useRef<HTMLButtonElement>(null)
  const flyoutClose = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (flyout) flyoutClose.current?.focus()
  }, [flyout])

  const closeFlyout = useCallback(() => {
    setFlyout(false)
    // It was only covered, so it can take the focus straight back.
    flyoutButton.current?.focus()
  }, [])

  if (reopenId && (reopenState.error || arranged)) {
    // The deep link is dead (no record and no 3MF to read it from), or names an
    // arranged output. /edit/{id} owns both messages; sending the reader there keeps
    // one copy of each.
    return <Navigate to={editPath(reopenId)} replace />
  }

  if (foreign) {
    return (
      <Navigate
        to={`${modelPath(foreign.slug)}?from=${foreign.output_id}`}
        state={{ editTarget: foreign } satisfies EditNavigationState}
        replace
      />
    )
  }

  // Reopened inputs wait for their migration: the panel must not paint defaults first.
  if (schemaState.loading || migrating) {
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

  // The model's own name (#179): what Edit details renames, and what the page
  // shows once its record is in. Not `schema.title`: that is the .scad file OpenSCAD
  // exported, "model" for every model (#939), so the slug stands in until then.
  const displayName = modelState.data?.name ?? slug

  const originLabel =
    record?.origin === 'builtin'
      ? 'built-in'
      : record?.origin_url
        ? importedFrom(record.origin_url)
        : 'mine'

  // One element, in the workspace or in the template's <sb-preview>: only one of them is
  // ever mounted (sb-preview renders it only in the page slot, where the workspace does not).
  const previewElement = (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center bg-bg text-[13px] text-faint">
          Loading the viewer
        </div>
      }
    >
      <Preview
        job={job}
        rendering={rendering || !settled}
        stage={renderStage}
        plate={plate}
        captureRef={captureRef}
        sourceLink={
          origin && (
            <Link to={modelPath(slug, 'source')}>{origin === 'builtin' ? 'View source' : 'Edit source'}</Link>
          )
        }
        leading={
          // The page slot has no parameters flyout: the template's own page is the panel.
          full && customUi?.slot !== 'page' && (
            <ParametersButton
              ref={flyoutButton}
              open={flyout}
              flyout={FLYOUT_ID}
              onClick={() => setFlyout((open) => !open)}
            />
          )
        }
        controls={<FullscreenButton active={full} onClick={fullscreen.toggle} />}
        // The flyout lies over the scene; the readouts move clear of it.
        covered={full && flyout ? FLYOUT_WIDTH : undefined}
        rejected={Boolean(renderError)}
      />
    </Suspense>
  )

  const elementContext = {
    schema,
    slug,
    version,
    fonts: fontsState.data ?? [],
    inputs,
    getInputs: hostDeps.getInputs,
    onInputs: hostDeps.setInputs,
    preview: previewElement,
    generate: (
      <span className="inline-flex items-center gap-2">
        <Button
          // `hostDeps.generate` runs one save at a time and keeps `uiGenerate` (this
          // button's state and its error) for every caller.
          onClick={() => void hostDeps.generate().catch(() => undefined)}
          disabled={uiGenerate.generating || rendering || !settled || Boolean(invalid) || job?.status !== 'done'}
        >
          {uiGenerate.generating
            ? 'Generating…'
            : rendering || !settled
              ? renderStage
                ? `Rendering: ${renderStage}`
                : 'Rendering…'
              : 'Generate'}
        </Button>
        {uiGenerate.error && (
          <span role="alert" className="text-[12px] text-warn">
            Could not generate: {uiGenerate.error}
          </span>
        )}
      </span>
    ),
  }

  const actionBar = (
    <ActionBar
      ref={actions}
      slug={slug}
      job={job}
      rendering={rendering || !settled}
      upToDate={upToDate}
      output={output}
      capture={capture}
      captureImage={captureImage}
      viewSize={viewSize}
      cameraView={cameraView}
      model={modelState.data}
      onModelChanged={modelState.setData}
      extra={extra}
      fit={fit}
      fitProblems={misfit}
      onPrinterModel={setPrinterModel}
      onGenerated={(created, savedExtra) => {
        if (job) setSaved({ jobId: job.id, extra: savedExtra, output: created })
        outputsState.reload()
      }}
      onSent={() => outputsState.reload()}
      onRan={() => outputsState.reload()}
    />
  )

  const uiOrigin = (
    <span data-testid="ui-origin" className="ml-auto shrink-0 text-[11px] text-faint">
      Custom interface · {originLabel}
    </span>
  )
  const uiPresets = (
    <PresetPicker
      key={`${slug}:${presetsRevision}`}
      slug={slug}
      schema={schema}
      values={values}
      extra={extra}
      onApply={onApplyPreset}
      migrate={migratePreset}
      pinned={version !== undefined}
      resetKey={resets}
    />
  )
  const templateUi = customUi && (
    <TemplateUi
      slug={slug}
      ui={customUi}
      version={uiVersion}
      deps={hostDeps}
      inputs={inputs}
      onFailure={(next) => setUiFailure({ slug, version, failure: next })}
      elementContext={elementContext}
    />
  )

  return (
    // #971 — `short:` scrolls the stacked page on a short window; full screen is the view
    // alone, so none of it applies there. #362 — minmax(0, 1fr), not the implicit auto
    // column: an auto track grows to its widest child's min-content (a long preset name,
    // a row of slider boxes), which on a phone held every pane wider than the screen.
    <div
      className={`grid h-full min-h-0 grid-cols-[minmax(0,1fr)] grid-rows-[auto_auto_minmax(0,1fr)] ${full ? '' : 'short:block short:overflow-y-auto'}`}
    >
      {/* Wraps rather than running off the right edge on a narrow (or zoomed) window (#971, #362). */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line bg-surface px-3 py-1.5">
        <div className="flex min-w-0 items-baseline gap-2">
          <Link to="/" className="shrink-0 text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <h1 className="truncate text-[13px] font-medium">{displayName}</h1>
          <DuplicatedFrom upstream={modelState.data?.upstream} className="shrink-0" />
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
        <div className="flex max-w-full shrink-0 flex-wrap items-center gap-1">
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
          {origin === 'builtin' && (
            <span
              data-testid="builtin-badge"
              className="shrink-0 rounded-[6px] bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted"
            >
              Built-in template — read-only
            </span>
          )}
          {origin === 'mine' && (
            <UpstreamUpdateButton
              slug={slug}
              state={modelState.data?.upstream_state}
              // Every action rewrites the record. Only a merge changes the source, so only
              // a merge re-reads the schema (which re-seeds the values and re-renders).
              onChanged={(action) => {
                modelState.reload()
                if (action === 'merge') schemaState.reload()
              }}
            />
          )}
          {origin && (
            <Link
              to={modelPath(slug, 'source')}
              className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
            >
              {origin === 'builtin' ? 'View source' : 'Edit source'}
            </Link>
          )}
          <Link
            to={modelPath(slug, 'versions')}
            className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
          >
            Versions
          </Link>
          <Link
            to={modelPath(slug, 'history')}
            className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
          >
            History
            {outputsState.data && outputsState.data.length > 0 && (
              <span className="sb-num ml-1.5 text-faint">{outputsState.data.length}</span>
            )}
          </Link>
          <Link
            to={modelPath(slug, 'prints')}
            className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
          >
            Prints
          </Link>
          {/* #931 — the assistant sessions that changed this model, and the output reopened here. */}
          <ResourceSessions resource={{ type: 'model', id: slug }} model={slug} />
          {reopenId && (
            <ResourceSessions
              resource={{ type: 'output', id: reopenId }}
              model={slug}
              label="Output changed by assistant"
            />
          )}
          {modelState.data && origin && (
            <MediaButton model={modelState.data} onChanged={modelState.setData} />
          )}
          {origin && (
            <DuplicateModelButton slug={slug} name={displayName} />
          )}
          {origin === 'mine' && (
            <>
              <ModelLibrariesButton
                slug={slug}
                name={displayName}
                // The library path changes what the source resolves to: re-read the
                // schema, which re-seeds the values and so re-renders the preview.
                onSaved={schemaState.reload}
              />
              {/* #179: only a template of mine is writable, so its details are too. */}
              <EditDetailsButton
                slug={slug}
                // The record this page holds (its name feeds Duplicate's prefill) is
                // stale after a save. The save answers with the new one, so it is
                // taken as it is: no refetch, and no flicker of the actions while
                // the record reloads.
                onSaved={modelState.setData}
              />
              <DeleteModelButton slug={slug} name={displayName} />
            </>
          )}
        </div>
      </div>

      {/* The write actions wait on the record, so a failed fetch has to say so. */}
      <div>
        {sourceChanged && (
          <div
            role="status"
            className="flex items-center gap-3 border-b border-accent/40 bg-accent/8 px-3 py-2 text-[12px]"
          >
            <span>This model&apos;s source changed elsewhere. Reload its parameters? Your changes here are kept until you do.</span>
            <Button size="sm" onClick={reloadSchema}>
              Reload parameters
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSourceChangedFor(null)}>
              Keep mine
            </Button>
          </div>
        )}
        {modelState.error && (
          <div
            role="alert"
            className="flex items-center gap-3 border-b border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn"
          >
            <span>Could not load this model&apos;s details: {modelState.error.message}</span>
            <Button size="sm" onClick={reloadModel}>
              Try again
            </Button>
          </div>
        )}
        {/* A malformed declaration arrives as `ui: null` plus `ui_error`: banner that too. */}
        {failure && (declared || record?.ui_error) && (
          <div
            role="alert"
            aria-label="Template interface failed"
            className="flex items-center gap-3 border-b border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn"
          >
            <span>
              This template&apos;s own interface ({failure.file}) could not start: {failure.message}. Showing the
              generated form instead.
            </span>
            <Button size="sm" onClick={retryUi}>
              Try again
            </Button>
          </div>
        )}
      </div>

      {!choosing && customUi?.slot === 'page' ? (
        <div
          ref={workspace}
          data-testid="workspace"
          // As the panel layout: where the Fullscreen API is refused (inside Bambuddy's
          // frame) the `window` mode is this element covering the window.
          className={`grid min-h-0 grid-cols-[minmax(0,1fr)] grid-rows-[auto_minmax(0,1fr)_auto] ${
            full ? `bg-bg ${fullscreen.mode === 'window' ? 'fixed inset-0 z-40' : 'relative'}` : ''
          }`}
        >
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-1.5">
            {uiPresets}
            {uiOrigin}
          </div>
          {templateUi}
          {actionBar}
        </div>
      ) : (
      <div
        ref={workspace}
        data-testid="workspace"
        className={`grid min-h-0 grid-cols-[minmax(0,1fr)] ${
          full
            ? `bg-bg [--sb-flyout:100%] md:[--sb-flyout:360px] ${
                fullscreen.mode === 'window' ? 'fixed inset-0 z-40' : 'relative'
              }`
            : 'lg:grid-cols-[minmax(300px,360px)_minmax(0,1fr)]'
        }`}
      >
        <div
          id={FLYOUT_ID}
          hidden={full && !flyout}
          className={
            full
              ? 'absolute inset-y-0 left-0 z-20 w-(--sb-flyout) shadow-2xl'
              : 'min-h-0 min-w-0 stacked-tall:max-h-[45vh] max-lg:border-b max-lg:border-line'
          }
        >
          {unmigrated && <RawInputs inputs={unmigrated.inputs} error={unmigrated.error} />}
          {choosing ? null : templateUi ? (
            <div className="flex h-full min-h-0 flex-col">
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-1.5">
                {full && <FlyoutHeader ref={flyoutClose} onClose={closeFlyout} />}
                {uiPresets}
                {uiOrigin}
              </div>
              {templateUi}
            </div>
          ) : (
            <ParameterPanel
              schema={schema}
              slug={slug}
              version={version}
              values={values}
              fonts={fontsState.data ?? []}
              onChange={onChange}
              onReset={onReset}
              reveal={reveal}
              rendered={renderedOutput}
              growsWithPage={!full}
              toolbar={
                <>
                  {full && <FlyoutHeader ref={flyoutClose} onClose={closeFlyout} />}
                  <PresetPicker
                    // A preset picked on one model means nothing on the next; a preset a
                    // template UI saved (presetsRevision) must show in the list too.
                    key={`${slug}:${presetsRevision}`}
                    slug={slug}
                    schema={schema}
                    values={values}
                    extra={extra}
                    onApply={onApplyPreset}
                    migrate={migratePreset}
                    pinned={version !== undefined}
                    resetKey={resets}
                  />
                </>
              }
            />
          )}
        </div>

        <div
          className={`grid min-h-0 min-w-0 grid-cols-[minmax(0,1fr)] grid-rows-[minmax(0,1fr)_auto] ${full ? '' : 'short:grid-rows-[max(16rem,60vh)_auto]'}`}
        >
          {/* #280 — the template's media beside the preview; nothing at all without any. */}
          <PreviewGallery slug={slug} media={modelState.data?.media} label={displayName} hidden={full}>
            {/* Not before the layout is chosen: a page-slot template's preview moves into
                its own page, which would mount the viewer a second time. */}
            {choosing ? null : previewElement}
          </PreviewGallery>
          {misfit.length > 0 && (
            <p
              role="status"
              data-testid="plate-fit"
              className="border-t border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn"
            >
              Does not fit: {misfit.join('; ')}.
            </p>
          )}
          {renderBusy !== undefined && (
            <p
              role="status"
              data-testid="render-busy"
              className="border-t border-line px-3 py-2 text-[12px] text-muted"
            >
              {renderBusyText(renderBusy)}
            </p>
          )}
          {/* A template UI draws its own fields, so it is not the panel that flags the value. */}
          {invalid && templateUi && (
            <p role="alert" className="border-t border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn">
              {invalid}
            </p>
          )}
          {renderError && (
            <p role="alert" className="border-t border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn">
              {renderError.message}
              {canRetry(renderError) && (
                <>
                  {' '}
                  <Button size="sm" onClick={retryRender}>
                    Try again
                  </Button>
                </>
              )}
            </p>
          )}
          {/* Full screen is the view and its parameters; the actions wait outside it. */}
          <div hidden={full}>
            {actionBar}
          </div>
        </div>
      </div>
      )}
    </div>
  )
}
