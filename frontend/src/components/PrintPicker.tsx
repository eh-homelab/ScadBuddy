import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type {
  FilamentOptions,
  Output,
  PipelineReport,
  PipelineChoices,
  PipelineView,
  PrintRunRequest,
  PrintRunResult,
  SlotChoice,
} from '../api/types'
import { openExternal } from '../lib/embed'
import { eligibilityIssues, verdictFor, type Verdict } from '../lib/problems'
import { FilamentPicker } from './FilamentPicker'
import { NewPipelineForm } from './NewPipelineForm'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/**
 * The print picker (#86): choose one of Bambuddy's slicer pipelines — or build one — and
 * run it for this output.
 *
 * Three things about Bambuddy's model shape this panel:
 *
 * - Eligibility is judged against an **uploaded library file**, so opening the panel
 *   uploads the 3MF (once — an output is immutable) and then asks each pipeline. An
 *   ineligible answer is a 200 carrying the report, so a row can be greyed out with its
 *   reasons rather than blowing up on Run.
 * - A pipeline's **target** is either one printer or a printer *class*. Where it is a
 *   class with more than one printer, the panel asks which printer, because that is the
 *   only way to know whose `printer_reports` entry to show.
 * - `PipelineRunRequest` carries **no printer**, so the chosen printer scopes what is
 *   *shown*; Bambuddy still fans out by the pipeline's own `fanout_strategy` and reports
 *   the printer per copy in `run.jobs[]`.
 *
 * Deliberately out of scope, each its own issue: filament/AMS slot mapping (#87), the
 * rest of `PrintQueueItemCreate` as an options disclosure (#88), and following the run to
 * completion (#89). This panel runs the pipeline and reports what Bambuddy answered.
 */

const MAX_COPIES = 50

/**
 * Choosing a plate is #83. Until then every print is plate 1, which is also the
 * server's own default — so sending it explicitly changes nothing about what runs and
 * keeps the request shape the same whichever route it takes.
 */
const PLATE_ID = 1

function targetLabel(pipeline: PipelineView): string {
  if (pipeline.target_kind === 'specific_printer') {
    return pipeline.target_printer_name ?? `printer #${pipeline.target_printer_id ?? '?'}`
  }
  return pipeline.target_model_class ? `any ${pipeline.target_model_class}` : 'any printer'
}

function presetSummary(pipeline: PipelineView): string {
  const names = [
    pipeline.process_preset_name ?? pipeline.process_preset?.id,
    ...(pipeline.filament_preset_names ?? []).map(
      (name, slot) => name ?? pipeline.filament_presets?.[slot]?.id,
    ),
  ].filter(Boolean)
  return names.join(' · ')
}

interface Props {
  open: boolean
  slug: string
  output: Output | undefined
  onClose: () => void
  onRan: (result: PrintRunResult) => void
}

export function PrintPicker({ open, slug, output, onClose, onRan }: Props) {
  const [choices, setChoices] = useState<PipelineChoices | null>(null)
  // Keyed by pipeline id, and holding the whole row: a pipeline Bambuddy could not judge
  // arrives with `error` set and no `report`, which is neither ready nor blocked.
  const [reports, setReports] = useState<Record<number, PipelineReport>>({})
  const [selected, setSelected] = useState<number | null>(null)
  const [printerId, setPrinterId] = useState<number | null>(null)
  const [copies, setCopies] = useState(1)
  const [asDefault, setAsDefault] = useState(false)
  const [force, setForce] = useState(false)
  const [creating, setCreating] = useState(false)
  /** #87 — the inventory behind the filament picker, and the plan built on it. */
  const [filaments, setFilaments] = useState<FilamentOptions | null>(null)
  const [filamentError, setFilamentError] = useState<string | null>(null)
  const [plan, setPlan] = useState<SlotChoice[]>([])
  const [exact, setExact] = useState(false)

  const [loading, setLoading] = useState(false)
  const [checking, setChecking] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [runIssues, setRunIssues] = useState<string[]>([])
  const [result, setResult] = useState<PrintRunResult | null>(null)

  const outputId = output?.id
  /**
   * Supersedes an in-flight load or check. The panel is not unmounted when it closes —
   * `ActionBar` renders it always and `Dialog` only drops its children — so a request
   * started for one output can still resolve after the panel has been reopened for
   * another, and would otherwise overwrite the newer answer with the older one.
   */
  const attempt = useRef(0)

  /** ``prefer`` selects a pipeline the caller has just created. */
  const load = useCallback(async (prefer?: number) => {
    const token = (attempt.current += 1)
    setLoading(true)
    setError(null)
    try {
      const next = await api.getModelPipelines(slug)
      if (token !== attempt.current) return
      setChoices(next)
      const ids = (next.pipelines ?? []).map((pipeline) => pipeline.id)
      setSelected((current) => {
        if (prefer !== undefined && ids.includes(prefer)) return prefer
        if (current !== null && ids.includes(current)) return current
        if (next.default_pipeline_id && ids.includes(next.default_pipeline_id)) {
          return next.default_pipeline_id
        }
        return ids[0] ?? null
      })
    } catch (cause) {
      if (token !== attempt.current) return
      setError(cause instanceof ApiError ? cause.detail : 'Could not list the pipelines.')
    } finally {
      if (token === attempt.current) setLoading(false)
    }
  }, [slug])

  const check = useCallback(async () => {
    if (!outputId) return
    const token = attempt.current
    setChecking(true)
    try {
      // This uploads the 3MF if Bambuddy has not got it: there is no eligibility answer
      // before a library file exists.
      const overview = await api.checkEligibility(outputId)
      if (token !== attempt.current) return
      setReports(
        Object.fromEntries((overview.reports ?? []).map((entry) => [entry.pipeline_id, entry])),
      )
    } catch (cause) {
      if (token !== attempt.current) return
      setError(cause instanceof ApiError ? cause.detail : 'Could not check eligibility.')
    } finally {
      if (token === attempt.current) setChecking(false)
    }
  }, [outputId])

  useEffect(() => {
    if (!open) return
    void load().then(() => check())
  }, [open, load, check])

  // The reports describe one output's 3MF, so they do not survive a change of output.
  useEffect(() => {
    setReports({})
  }, [outputId])

  /**
   * The "make this the default" tick follows the selection: it means "the pipeline in view
   * *is* this model's default", not "this model has one". Without that, opening the picker
   * on a model that already has a default and switching pipelines for a single print would
   * silently re-point the default at whatever was selected last. A toggle the user makes
   * afterwards stands, because `asDefault` is not itself a dependency here.
   */
  useEffect(() => {
    setAsDefault(selected !== null && selected === (choices?.model_pipeline_id ?? null))
  }, [selected, choices])

  const pipelines = choices?.pipelines ?? []
  const current = pipelines.find((pipeline) => pipeline.id === selected)
  const entry = selected === null ? undefined : reports[selected]
  const report = entry?.report ?? undefined
  // A class target with more than one printer is the case Bambuddy cannot answer for us.
  const asksForPrinter = Boolean(
    current && current.target_kind === 'printer_class' && (current.printer_ids ?? []).length > 1,
  )
  const derivedPrinterId = asksForPrinter ? printerId : (current?.printer_ids?.[0] ?? null)
  const verdict: Verdict | undefined = report ? verdictFor(report, derivedPrinterId) : undefined

  /**
   * #87 — the inventory, read once a pipeline and (for a class target) a printer are
   * settled. It needs the printer: `loaded` means "loaded in *this* machine", and a
   * spool's reachability is a property of that printer's filament switcher, so asking
   * before one is chosen would answer about the wrong hardware.
   *
   * Its own attempt counter rather than the panel's: `printerId` and `selected` change
   * without touching `attempt`, so two reads can be in flight for the same output and
   * the older one must not land last.
   */
  const filamentAttempt = useRef(0)
  const awaitingPrinter = asksForPrinter && printerId === null
  useEffect(() => {
    if (!open || !outputId || selected === null || awaitingPrinter) {
      setFilaments(null)
      setPlan([])
      // Cleared too: the message names an output and a printer, so leaving it up while
      // the panel shows a different one attributes the failure to the wrong thing.
      setFilamentError(null)
      return
    }
    const token = (filamentAttempt.current += 1)
    setFilamentError(null)
    void (async () => {
      try {
        const next = await api.getFilaments(outputId, {
          printerId: derivedPrinterId,
          pipelineId: selected,
          plateId: PLATE_ID,
        })
        if (token !== filamentAttempt.current) return
        setFilaments(next)
        setFilamentError(null)
        // The server's auto-match seeds the selection; every slot stays editable.
        setPlan((next.suggested ?? []).map((choice) => ({ ...choice })))
        // A different printer makes the escalation mean something different — it names
        // the machine the copies land on — so the consent is asked for again.
        setExact(false)
      } catch (cause) {
        if (token !== filamentAttempt.current) return
        setFilaments(null)
        setPlan([])
        setFilamentError(
          cause instanceof ApiError ? cause.detail : 'Could not read the filament inventory.',
        )
      }
    })()
  }, [open, outputId, selected, derivedPrinterId, awaitingPrinter])

  /**
   * Whether the user has moved a slot off the server's suggestion. That is what makes
   * sending a plan *meaningful*: re-sending the suggestion would escalate an otherwise
   * ordinary pipeline run onto the slice-and-queue route for no gain.
   */
  const suggested = filaments?.suggested ?? []
  const planChanged =
    plan.length !== suggested.length ||
    suggested.some(
      (choice) =>
        plan.find((entry) => entry.slot_id === choice.slot_id)?.spool_id !== choice.spool_id,
    )
  const sendsPlan = filaments !== null && (exact || planChanged)
  // `force` is only offered once the issues have actually been shown.
  const issuesShown = (verdict !== undefined && !verdict.ok) || runIssues.length > 0
  const printers = (choices?.printers ?? []).filter((printer) =>
    (current?.printer_ids ?? []).includes(printer.id),
  )
  /**
   * A failure that hit *every* pipeline — a refused API key, an unreachable Bambuddy — is
   * one problem, not one per row. Repeating it against each pipeline would read as three
   * pipeline-specific faults and bury the actual cause.
   */
  const checked = pipelines.filter((pipeline) => reports[pipeline.id] !== undefined)
  const everyCheckFailed =
    checked.length > 0 && checked.every((pipeline) => !reports[pipeline.id]?.report)
  const wholeCheckError = everyCheckFailed
    ? (reports[checked[0]?.id ?? 0]?.error ?? 'Bambuddy gave no reason')
    : null

  function close() {
    setError(null)
    setRunIssues([])
    setResult(null)
    setForce(false)
    setCreating(false)
    onClose()
  }

  async function run() {
    if (!outputId || selected === null) return
    setRunning(true)
    setError(null)
    setRunIssues([])
    try {
      // The stored default only moves on a real change of intent: ticking the box on a
      // pipeline that is not already the default, or unticking it on the one that is.
      // Printing something else once says nothing about what this model should default
      // to, so neither switching pipelines nor leaving the box alone writes anything.
      const stored = choices?.model_pipeline_id ?? null
      const remember = async (pipelineId: number | null) => {
        await api.putModelPipeline(slug, pipelineId)
        // Functional, and never spread over a null: `selected` can only be non-null once
        // `choices` has loaded, so this is unreachable today — but a spread of null would
        // silently drop `pipelines` and `printers` and empty the list.
        setChoices((current) =>
          current ? { ...current, model_pipeline_id: pipelineId } : current,
        )
      }
      if (asDefault && stored !== selected) await remember(selected)
      else if (!asDefault && stored === selected) await remember(null)
      const body: PrintRunRequest = {
        pipeline_id: selected,
        copies,
        force,
        plate_id: PLATE_ID,
      }
      /**
       * #87 — naming a printer or a filament plan is what escalates this off the
       * pipeline route: `PipelineRunCreateRequest` can express neither, so the backend
       * has to slice the library file and post queue entries instead. That changes
       * which printer the copies land on, so it is only done when the user has actually
       * asked — by moving a slot, or by ticking the box that says so.
       */
      if (sendsPlan) {
        body.printer_id = derivedPrinterId
        body.filament_plan = { slots: plan, force_colour_match: false }
      }
      const ran = await api.runPipeline(outputId, body)
      setResult(ran)
      onRan(ran)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The print could not be started.')
      // A 409 carries Bambuddy's report verbatim; list what blocked it so Run anyway is
      // an informed choice rather than a shrug.
      setRunIssues(cause instanceof ApiError ? eligibilityIssues(cause.problem) : [])
    } finally {
      setRunning(false)
    }
  }

  return (
    <Dialog
      open={open}
      title="Print"
      description={
        result || creating ? undefined : 'Bambuddy slices and queues this with the pipeline below.'
      }
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            <Button variant="primary" onClick={() => openExternal(result.bambuddy_url)}>
              Open in queue
            </Button>
          </>
        ) : creating ? undefined : (
          <>
            <Button onClick={close} disabled={running}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void run()}
              disabled={running || selected === null || (asksForPrinter && printerId === null)}
              data-testid="run-pipeline"
            >
              {running && <Spinner />}
              {force ? 'Run anyway' : 'Run'}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <div className="space-y-2 text-[13px] text-ink">
          {/* `run` is null on the slice-and-queue route — there is no pipeline run to
              report there, only a slice job and the queue entries it produced. */}
          {result.run && (
            <>
              <p>
                Pipeline run <span className="sb-num">#{result.run.id}</span> started for{' '}
                <span className="sb-num">{result.run.copies}</span>{' '}
                {result.run.copies === 1 ? 'copy' : 'copies'}.
              </p>
              {(result.run.jobs ?? []).length > 0 && (
                <ul className="space-y-0.5 text-[12px] text-muted" data-testid="run-jobs">
                  {(result.run.jobs ?? []).map((job) => (
                    <li key={job.id}>
                      Copy <span className="sb-num">{job.copy_index + 1}</span> on{' '}
                      {job.assigned_printer_name ?? 'a printer Bambuddy picks'}
                      {job.queue_entry_id ? (
                        <>
                          {' '}
                          as queue <span className="sb-num">#{job.queue_entry_id}</span>
                        </>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
              {result.run.eligibility_overridden && (
                <p className="text-[12px] text-warn">
                  Started with the eligibility check overridden.
                </p>
              )}
            </>
          )}
          {result.route === 'slice_queue' && (
            <div data-testid="queued-items">
              <p>
                Sliced and queued for{' '}
                {filaments?.printer_name ?? 'the printer you chose'} —{' '}
                <span className="sb-num">{(result.queue_item_ids ?? []).length}</span>{' '}
                {(result.queue_item_ids ?? []).length === 1 ? 'item' : 'items'}.
              </p>
              {(result.queue_item_ids ?? []).length > 0 && (
                <ul className="mt-1 space-y-0.5 text-[12px] text-muted">
                  {(result.queue_item_ids ?? []).map((itemId) => (
                    <li key={itemId}>
                      Queue <span className="sb-num">#{itemId}</span>
                    </li>
                  ))}
                </ul>
              )}
              {result.slice_job_id !== null && result.slice_job_id !== undefined && (
                <p className="mt-1 text-[12px] text-faint">
                  Slice job <span className="sb-num">#{result.slice_job_id}</span>.
                </p>
              )}
            </div>
          )}
          {(result.warnings ?? []).length > 0 && (
            <ul className="space-y-0.5 text-[12px] text-muted" data-testid="run-warnings">
              {(result.warnings ?? []).map((warning, index) => (
                <li key={`${warning.kind}-${index}`}>{warning.message}</li>
              ))}
            </ul>
          )}
          {/* Following the run to completion is #89; this reports what Bambuddy answered. */}
        </div>
      ) : creating ? (
        <NewPipelineForm
          colors={output?.colors ?? []}
          onCancel={() => setCreating(false)}
          onCreated={(pipeline) => {
            setCreating(false)
            setSelected(pipeline.id)
            void load(pipeline.id).then(() => check())
          }}
        />
      ) : (
        <>
          {loading && (
            <p className="flex items-center gap-2 text-[13px] text-muted">
              <Spinner /> Loading pipelines
            </p>
          )}

          {!loading && pipelines.length === 0 && (
            <p className="text-[13px] text-muted">
              Bambuddy has no slicer pipelines yet. Create one and ScadBuddy will remember it
              for this model.
            </p>
          )}

          {pipelines.length > 0 && (
            <fieldset>
              <legend className="sr-only">Pipeline</legend>
              <ul className="space-y-2" data-testid="pipeline-list">
                {pipelines.map((pipeline) => {
                  const own = reports[pipeline.id]
                  // The selected row is narrowed to the printer in play; the others are
                  // shown as the class as a whole, since no printer has been chosen for them.
                  const rowVerdict = own?.report
                    ? verdictFor(own.report, pipeline.id === selected ? derivedPrinterId : null)
                    : undefined
                  return (
                    <li key={pipeline.id}>
                      <label
                        className={`flex cursor-pointer gap-2.5 rounded-[6px] border p-3 transition-colors ${
                          selected === pipeline.id
                            ? 'border-accent bg-accent/8'
                            : 'border-line bg-surface-2 hover:border-line-strong'
                        }`}
                      >
                        <input
                          type="radio"
                          name="print-pipeline"
                          value={pipeline.id}
                          checked={selected === pipeline.id}
                          onChange={() => {
                            setSelected(pipeline.id)
                            setPrinterId(null)
                            setForce(false)
                            setRunIssues([])
                          }}
                          className="mt-0.5 accent-[var(--sb-accent)]"
                        />
                        <span className="min-w-0">
                          <span className="flex items-center gap-2">
                            <span className="text-[13px] text-ink">{pipeline.name}</span>
                            {rowVerdict && (
                              <span
                                className={`text-[11px] ${rowVerdict.ok ? 'text-ok' : 'text-warn'}`}
                              >
                                {rowVerdict.ok ? 'ready' : 'not ready'}
                              </span>
                            )}
                            {own && !own.report && (
                              <span className="text-[11px] text-faint">not checked</span>
                            )}
                          </span>
                          <span className="mt-0.5 block text-[12px] text-muted">
                            {targetLabel(pipeline)}
                            {pipeline.bed_type ? ` · ${pipeline.bed_type}` : ''}
                          </span>
                          {presetSummary(pipeline) && (
                            <span className="mt-0.5 block truncate text-[12px] text-faint">
                              {presetSummary(pipeline)}
                            </span>
                          )}
                          {own && !own.report && !everyCheckFailed && (
                            <span
                              className="mt-1 block text-[12px] text-faint"
                              data-testid={`uncheckable-${pipeline.id}`}
                            >
                              Bambuddy could not check this pipeline:{' '}
                              {own.error || 'it gave no reason'}
                            </span>
                          )}
                          {rowVerdict && rowVerdict.issues.length > 0 && (
                            <ul
                              className={`mt-1 list-disc space-y-0.5 pl-4 text-[12px] ${
                                rowVerdict.ok ? 'text-muted' : 'text-warn'
                              }`}
                              data-testid={`issues-${pipeline.id}`}
                            >
                              {rowVerdict.issues.map((issue) => (
                                <li key={issue}>{issue}</li>
                              ))}
                            </ul>
                          )}
                        </span>
                      </label>
                    </li>
                  )
                })}
              </ul>
            </fieldset>
          )}

          {wholeCheckError && (
            <p
              role="status"
              className="mt-2 text-[12px] text-warn"
              data-testid="eligibility-unavailable"
            >
              Bambuddy could not check any of these pipelines: {wholeCheckError}
            </p>
          )}

          {checking && (
            <p className="mt-2 flex items-center gap-2 text-[12px] text-muted">
              <Spinner /> Checking eligibility
            </p>
          )}

          <div className="mt-3">
            <Button size="sm" onClick={() => setCreating(true)} data-testid="new-pipeline">
              New pipeline
            </Button>
          </div>

          {asksForPrinter && (
            <div className="mt-4">
              <label htmlFor="print-printer" className="block text-[13px]">
                Printer
              </label>
              <select
                id="print-printer"
                value={printerId === null ? '' : String(printerId)}
                onChange={(event) =>
                  setPrinterId(event.target.value === '' ? null : Number(event.target.value))
                }
                className="sb-field mt-1.5 cursor-pointer"
              >
                <option value="">Choose a printer</option>
                {printers.map((printer) => (
                  <option key={printer.id} value={printer.id}>
                    {printer.name}
                    {printer.model ? ` (${printer.model})` : ''}
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-[12px] text-muted">
                This pipeline targets a printer class, so ScadBuddy shows that
                printer&rsquo;s readiness. Bambuddy still assigns the copies itself.
              </p>
            </div>
          )}
          {!asksForPrinter && verdict?.printerName && (
            <p className="mt-3 text-[12px] text-muted">
              Printing on <span className="text-ink">{verdict.printerName}</span>, from the
              pipeline&rsquo;s target.
            </p>
          )}

          <div className="mt-4 flex items-center gap-3">
            <label htmlFor="print-copies" className="text-[13px] text-ink">
              Copies
            </label>
            <input
              id="print-copies"
              type="number"
              min={1}
              max={MAX_COPIES}
              value={copies}
              onChange={(event) => setCopies(Math.max(1, Number(event.target.value)))}
              className="sb-field sb-num w-20 text-right"
            />
          </div>

          <label className="mt-3 flex cursor-pointer items-center gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={asDefault}
              onChange={(event) => setAsDefault(event.target.checked)}
              className="accent-[var(--sb-accent)]"
            />
            Always use this pipeline for this model
          </label>
          {!asDefault && choices?.global_pipeline_id ? (
            <p className="mt-1 text-[12px] text-faint">
              Otherwise the pipeline set in Settings is the fallback.
            </p>
          ) : null}

          {filamentError && (
            <p className="mt-3 text-[12px] text-muted" data-testid="filaments-unavailable">
              ScadBuddy could not read the filament inventory: {filamentError}. The pipeline
              will use its own filament presets.
            </p>
          )}

          {filaments && (
            <>
              <FilamentPicker
                options={filaments}
                plan={plan}
                onChange={setPlan}
                copies={copies}
              />
              {/**
               * Off by default, and it says what it costs. Ticking it pins the printer,
               * which is exactly what a pipeline run cannot express — so the backend
               * slices and queues instead, and a class-targeted pipeline stops fanning
               * out across its printers. Changing a slot implies the same thing and is
               * treated as the same consent, which is why the box is only the way to
               * ask for it *without* changing anything.
               */}
              <label className="mt-3 flex cursor-pointer items-start gap-2 text-[13px]">
                <input
                  type="checkbox"
                  checked={exact}
                  onChange={(event) => setExact(event.target.checked)}
                  className="mt-0.5 accent-[var(--sb-accent)]"
                  data-testid="use-exact-filaments"
                />
                <span>
                  Use exactly these spools — ScadBuddy will slice and queue this for{' '}
                  {filaments.printer_name ?? 'the chosen printer'}, instead of letting the
                  pipeline choose a printer.
                </span>
              </label>
              {planChanged && !exact && (
                <p className="mt-1 text-[12px] text-faint">
                  A slot has been changed, so this print will be sliced and queued for{' '}
                  {filaments.printer_name ?? 'the chosen printer'} either way.
                </p>
              )}
            </>
          )}

          {/* #88 adds the rest of PrintQueueItemCreate here as an options disclosure. */}

          {issuesShown && (
            <label className="mt-3 flex cursor-pointer items-center gap-2 text-[13px] text-warn">
              <input
                type="checkbox"
                checked={force}
                onChange={(event) => setForce(event.target.checked)}
                className="accent-[var(--sb-accent)]"
                data-testid="force"
              />
              Print anyway, ignoring the issues above
            </label>
          )}

          {error && (
            <div role="alert" className="mt-3 text-[13px] text-warn">
              <p>{error}</p>
              {runIssues.length > 0 && (
                <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-[12px]">
                  {runIssues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </Dialog>
  )
}
