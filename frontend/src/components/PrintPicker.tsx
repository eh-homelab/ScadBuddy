import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError, mayHaveRun } from '../api/client'
import type {
  ChoicesView,
  FilamentOptions,
  NozzleChoice,
  OutputPlate,
  Output,
  PresetRef,
  PrintChoices,
  PrintOptions,
  PrintOptionsState,
  PrintRunRequest,
  PrintRunResult,
  SlotChoice,
} from '../api/types'
import { openExternal } from '../lib/embed'
import { useAsync } from '../lib/useAsync'
import { seedPlan } from '../lib/filaments'
import { resolveOptions } from '../lib/printOptions'
import { usePrintProgress } from '../lib/usePrintProgress'
import { arrangedName, arrangedNote, GOAL_LABELS, runArrange, type ArrangeGoal } from '../lib/arrange'
import { FilamentPicker, WarningList } from './FilamentPicker'
import { NozzleStep } from './print/NozzleStep'
import { PlateStep } from './print/PlateStep'
import { QualityStep } from './print/QualityStep'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'
import { PrintProgressPanel } from './PrintProgressPanel'
import { ProjectPicker } from './ProjectPicker'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/**
 * The print dialog, spool-first (docs/superpowers/specs/2026-09-27-spool-first-print-design.md).
 *
 * You choose what you actually think about — the spools from the inventory, the nozzle
 * size, a quality tier and the plate — and ScadBuddy derives every Bambu preset from
 * those choices server-side, then slices and queues through Bambuddy. There are no
 * slicer pipelines here any more; they stay in Bambuddy untouched.
 *
 * - One read, `GET /print/outputs/{id}/choices`, opens the dialog: printers, installed
 *   nozzles, tiers and processes per size, plate types with the one last printed on,
 *   the filament step, and what this model last printed with (#78).
 * - One write, `POST /print/outputs/{id}/run`, prints. A 422 is the resolver refusing a
 *   combination (an unpicked slot, no process, no preset for a spool at this size); its
 *   `detail` is shown above Print and the dialog stays open.
 * - Simple mode offers the tiers; Advanced adds the full process list, per-side flow and
 *   a per-slot filament preset override.
 *
 * Around that it keeps what the send bar's print already had: the options disclosure
 * (#88), the project (#79), copies with the remembered quantity (#124/#145), which plate
 * of a multi-plate 3MF (#83), and following the run to completion (#89).
 */

const MAX_COPIES = 50

type Tier = NonNullable<PrintChoices['tier']>

const DEFAULT_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'standard' },
  { size: '0.4', flow: 'standard' },
]

function refKey(ref: PresetRef): string {
  return `${ref.source}:${ref.id}`
}

interface Props {
  open: boolean
  slug: string
  output: Output | undefined
  onClose: () => void
  onRan: (result: PrintRunResult) => void
  /** #81 — the model of the printer in view, so the preview can draw its plate. */
  onPrinterModel?: (model: string | null) => void
}

export function PrintPicker({ open, slug, output, onClose, onRan, onPrinterModel }: Props) {
  /** §7 — the output being printed: the one passed in, or what Re-arrange made of it. */
  const [target, setTarget] = useState<Output | undefined>(output)
  const [arrangeGoal, setArrangeGoal] = useState<ArrangeGoal>('fewest_swaps')
  const [arranging, setArranging] = useState(false)
  const [arrangeNote, setArrangeNote] = useState<string | null>(null)
  const [arrangeError, setArrangeError] = useState<string | null>(null)
  /**
   * What a re-arrange was made for, carried across the switch to its output: the new
   * file's slot N is the old one's colour (`colours` pins the order), so the plan and
   * the settings still apply. `ready` once the new output's choices are in.
   */
  const carry = useRef<{
    plan: SlotChoice[]
    overrides: Record<string, PresetRef>
    options: PrintOptions
    ready: boolean
  } | null>(null)
  /** The re-arrange in flight: closing the dialog (or unmounting it) stops its wait. */
  const arrangeRun = useRef<AbortController | null>(null)
  useEffect(() => {
    if (open) return
    arrangeRun.current?.abort()
    arrangeRun.current = null
  }, [open])
  useEffect(() => () => arrangeRun.current?.abort(), [])
  useEffect(() => {
    // A new output from the caller is not a re-arrange: nothing carries to it.
    carry.current = null
    setTarget(output)
  }, [output])
  const [choices, setChoices] = useState<ChoicesView | null>(null)
  /** The printer asked for; `null` lets the server open on the remembered one. */
  const [askedPrinter, setAskedPrinter] = useState<number | null>(null)
  const [nozzles, setNozzles] = useState<NozzleChoice[]>(DEFAULT_NOZZLES)
  const [tier, setTier] = useState<Tier | null>('standard')
  const [processName, setProcessName] = useState<string | null>(null)
  const [bedType, setBedType] = useState<string | null>(null)
  const [advanced, setAdvanced] = useState(false)
  /** Advanced only — a filament preset per slot id, in place of the spool's own. */
  const [overrides, setOverrides] = useState<Record<string, PresetRef>>({})

  /** #87 — the inventory behind the filament picker, and the plan built on it. */
  const [filaments, setFilaments] = useState<FilamentOptions | null>(null)
  const [plan, setPlan] = useState<SlotChoice[]>([])

  // null until the user sets it, so a remembered quantity is not overridden by the
  // box's own starting value (#124).
  const [copies, setCopies] = useState<number | null>(null)
  /** #145 — the remembered options, so the box can say what an unset Copies queues. */
  const [remembered, setRemembered] = useState<PrintOptionsState | null>(null)
  /** #88 — this print's overrides, all but `quantity`, which is `copies`. */
  const [options, setOptions] = useState<PrintOptions>({})
  /** #79 — the Bambuddy project this print is filed under. */
  const [projectId, setProjectId] = useState<number | null>(null)
  /** #83 — the output's plates, and the one (or all) to print. */
  const [plates, setPlates] = useState<OutputPlate[]>([])
  const [plate, setPlate] = useState<number | 'all'>(1)

  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filamentError, setFilamentError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  /** A refused run, shown above Print. */
  const [runError, setRunError] = useState<string | null>(null)
  /** Only a 422 — these choices cannot resolve — keeps Print disabled until one changes. */
  const [refused, setRefused] = useState(false)
  const [result, setResult] = useState<PrintRunResult | null>(null)
  /**
   * #470 — a run whose answer never arrived (a proxy's timeout, a dropped connection):
   * the backend may have queued it anyway, so the dialog says so instead of offering
   * Print again. Closing the dialog is the way back to it.
   */
  const [unanswered, setUnanswered] = useState<string | null>(null)
  // Only for the queue link while a run is unanswered (a result carries its own), so it
  // is read then, not on every open; a failed read offers to try again.
  const settings = useAsync(
    async () => (open && unanswered !== null ? await api.getSettings() : null),
    [open, unanswered !== null],
  )
  // As typed in Settings: a trailing slash would make `…//queue` below.
  const bambuddyUrl = settings.data?.bambuddy_url?.replace(/\/+$/, '') || null

  const outputId = target?.id
  /**
   * #89 — follow only the print this dialog just started, so opening the dialog on an
   * output printed last week does not start polling a run nobody is watching.
   */
  const { progress, polling } = usePrintProgress(outputId, open && result !== null)

  /**
   * #79 — file the finished print under its project, once the progress read (#89) has
   * the queue entries. Best effort, once per settled print, guarded by a ref so the
   * guard itself does not re-render and re-run the effect.
   */
  const attached = useRef<string | null>(null)
  useEffect(() => {
    if (!outputId || projectId === null || !progress?.settled) return
    const entries = (progress.copies_detail ?? [])
      .map((copy) => copy.queue_entry_id)
      .filter((id): id is number => typeof id === 'number')
    if (entries.length === 0) return
    const key = `${outputId}:${projectId}:${entries.join(',')}`
    if (attached.current === key) return
    attached.current = key
    void api
      .attachToProject(outputId, { project_id: projectId, queue_item_ids: entries })
      .catch(() => {
        attached.current = null
      })
  }, [outputId, projectId, progress])

  /**
   * Supersedes an in-flight read. The dialog is not unmounted when it closes, so a read
   * started for one output or printer can resolve after another has been asked for.
   */
  const attempt = useRef(0)
  /**
   * Spec §7 — the nozzles, tier and process this model last printed with are applied
   * once per open, on the first read: a later read for another printer must not undo
   * what the user has changed since.
   */
  const seeded = useRef(false)
  function seedDialog(last: ChoicesView['model_choices']) {
    const remembered = last?.nozzles ?? []
    const nextNozzles = remembered.length > 0 ? remembered : DEFAULT_NOZZLES
    const nextProcess = remembered.length > 0 ? (last?.process_name ?? null) : null
    const nextTier = nextProcess ? null : (last?.tier ?? 'standard')
    setNozzles(nextNozzles)
    setTier(nextTier)
    setProcessName(nextProcess)
    // A named process and per-side flow are Advanced choices; opening in Simple would
    // send them unseen.
    setAdvanced(nextProcess !== null || nextNozzles.some((n) => n.flow === 'high_flow'))
  }
  useEffect(() => {
    if (!open || !outputId) return
    const token = (attempt.current += 1)
    setLoading(true)
    setLoadError(null)
    api
      .getChoices(outputId, askedPrinter)
      .then((next) => {
        if (token !== attempt.current) return
        setChoices(next)
        // The server already applied last archive → remembered → default.
        setBedType(next.bed_type)
        if (carry.current) {
          // A re-arrange keeps the dialog's nozzles, tier, process and Advanced.
          carry.current.ready = true
        } else if (!seeded.current) {
          seeded.current = true
          seedDialog(next.model_choices)
        }
      })
      .catch((cause: unknown) => {
        if (token !== attempt.current) return
        // The arranged output's choices never arrived; its carry must not reach
        // whatever the dialog shows next.
        carry.current = null
        setChoices(null)
        setLoadError(cause instanceof ApiError ? cause.detail : 'Could not read the print choices.')
      })
      .finally(() => {
        if (token === attempt.current) setLoading(false)
      })
  }, [open, outputId, askedPrinter])

  // One output's plates and overrides do not survive a change of output, except to the
  // output a re-arrange made of it, which keeps the dialog's settings.
  useEffect(() => {
    setPlate(1)
    setPlates([])
    if (carry.current) return
    setOptions({})
    setOverrides({})
    seeded.current = false
  }, [outputId])

  useEffect(() => {
    if (!open || !outputId) return
    let live = true
    api
      .getOutputPlates(outputId)
      .then((next) => live && setPlates(next))
      // One plate is what every ScadBuddy render is, so an unreadable list asks nothing.
      .catch(() => live && setPlates([]))
    return () => {
      live = false
    }
  }, [open, outputId])

  const printerId = choices?.printer_id ?? null

  async function rearrange() {
    if (!target) return
    setArranging(true)
    setArrangeNote(null)
    setArrangeError(null)
    const controller = new AbortController()
    arrangeRun.current = controller
    try {
      const next = await runArrange(slug, {
        objects: (target.manifest ?? []).map((object) => ({
          output_id: target.id,
          part: object.part,
          count: object.count,
        })),
        goal: arrangeGoal,
        printer_id: printerId,
        filament_plan: { slots: plan, force_colour_match: false },
        colours: target.colors ?? [],
        name: arrangedName(target.name),
      }, { signal: controller.signal })
      carry.current = { plan, overrides, options, ready: false }
      setTarget(next.output)
      setArrangeNote(arrangedNote(next.plates))
    } catch (cause) {
      if (controller.signal.aborted) return
      setArrangeError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
    } finally {
      if (arrangeRun.current === controller) arrangeRun.current = null
      setArranging(false)
    }
  }
  const printers = choices?.printers ?? []
  const printer = printers.find((entry) => entry.id === printerId)
  const size = nozzles[0]?.size ?? '0.4'
  // One plan applies to every plate, a slot being the same color-numbered project
  // filament on each (#180). "All plates" reads every plate's slots, so a slot only a
  // later plate uses still gets a row (spec §2 step 1).
  const chosenPlate = plate === 'all' ? 1 : plate
  const allPlates = plate === 'all'
  const rememberedPlan = JSON.stringify(choices?.model_choices?.filament_plan ?? [])

  /**
   * The filament step: plate 1 is in the choices read already; another plate's slots
   * are that plate's own, and all plates' are their union, so those are read for it.
   */
  const filamentAttempt = useRef(0)
  useEffect(() => {
    const token = (filamentAttempt.current += 1)
    setFilamentError(null)
    if (!choices || !outputId) {
      setFilaments(null)
      setPlan([])
      return
    }
    const seed = (next: FilamentOptions) => {
      setFilaments(next)
      const carried = carry.current
      if (carried) {
        // A re-arrange: the plan it was made for, less any slot the new file lacks.
        setPlan(seedPlan(next, carried.plan))
        if (carried.ready) {
          const slots = new Set((next.slots ?? []).map((slot) => String(slot.slot_id)))
          setOverrides(Object.fromEntries(Object.entries(carried.overrides).filter(([id]) => slots.has(id))))
          setOptions(carried.options)
          carry.current = null
        }
        return
      }
      // What this model last printed with seeds the selection, else the server's
      // auto-match (#78); every slot stays editable.
      setPlan(seedPlan(next, JSON.parse(rememberedPlan) as SlotChoice[]))
    }
    if (chosenPlate === 1 && !allPlates) {
      seed(choices.filaments)
      return
    }
    api
      .getFilaments(
        outputId,
        allPlates
          ? { printerId: choices.printer_id ?? null, allPlates: true }
          : { printerId: choices.printer_id ?? null, plateId: chosenPlate },
      )
      .then((next) => token === filamentAttempt.current && seed(next))
      .catch((cause: unknown) => {
        if (token !== filamentAttempt.current) return
        setFilaments(null)
        setPlan([])
        setFilamentError(
          cause instanceof ApiError ? cause.detail : 'Could not read the filament inventory.',
        )
      })
  }, [choices, outputId, chosenPlate, allPlates, rememberedPlan])

  /** #81 — the chosen printer's model, reported once the choices have landed. */
  const printerModel = choices ? (printer?.model ?? null) : undefined
  useEffect(() => {
    if (printerModel !== undefined) onPrinterModel?.(printerModel)
  }, [printerModel, onPrinterModel])

  useEffect(() => {
    if (!open) return
    let live = true
    api
      .getPrintOptions()
      .then((view) => live && setRemembered(view))
      // Nothing to show is the pre-#145 behavior; the run still resolves it server-side.
      .catch(() => live && setRemembered(null))
    return () => {
      live = false
    }
  }, [open, slug])

  // A refused run was refused for *these* choices; any change is worth another try.
  useEffect(() => {
    setRunError(null)
    setRefused(false)
  }, [nozzles, tier, processName, bedType, plan, overrides, printerId, plate])

  const rememberedCopies =
    resolveOptions(
      remembered?.global_options,
      printerId === null ? undefined : remembered?.printers?.[String(printerId)],
      remembered?.models?.[slug],
    ).quantity ?? null
  const effectiveCopies = copies ?? rememberedCopies ?? 1

  const suggested = filaments?.suggested ?? []
  const planChanged =
    plan.length !== suggested.length ||
    suggested.some(
      (choice) =>
        plan.find((entry) => entry.slot_id === choice.slot_id)?.spool_id !== choice.spool_id,
    )

  /** Which `run()` may still update the dialog: bumped by each run. */
  const runAttempt = useRef(0)

  function close() {
    // Escape and the backdrop are ignored mid-run, as Cancel is: a closed dialog would
    // reopen with Print enabled and send the print a second time (#539 review).
    if (running) return
    setProjectId(null)
    setOptions({})
    setRunError(null)
    setRefused(false)
    setResult(null)
    setUnanswered(null)
    setAskedPrinter(null)
    setNozzles(DEFAULT_NOZZLES)
    setTier('standard')
    setProcessName(null)
    setAdvanced(false)
    setOverrides({})
    seeded.current = false
    onClose()
  }

  function changeNozzles(next: NozzleChoice[]) {
    // A preset chosen for one size is not one the other size takes.
    if (next[0]?.size !== size) {
      setOverrides({})
      setProcessName(null)
      setTier((current) => current ?? 'standard')
    }
    setNozzles(next)
  }

  function toggleAdvanced() {
    if (advanced) {
      // Back to Simple: a flow, named process or preset override would be sent unseen.
      setNozzles((current) => current.map((nozzle) => ({ ...nozzle, flow: 'standard' })))
      setProcessName(null)
      setTier((current) => current ?? 'standard')
      setOverrides({})
    }
    setAdvanced(!advanced)
  }

  function setOverride(slotId: number, key: string) {
    const ref = (choices?.filament_presets?.[size] ?? []).find((row) => refKey(row.ref) === key)
    setOverrides((current) => {
      const next = { ...current }
      if (ref) next[String(slotId)] = ref.ref
      else delete next[String(slotId)]
      return next
    })
  }

  /**
   * #78 / spec §7 — what this model reopens on next time: the printer, the nozzles,
   * tier and process, and the spools where they differ from the suggestion (re-sending
   * the suggestion is not a choice). Best effort, and only once the print has started.
   */
  function rememberChoices() {
    const last = choices?.model_choices
    const next = {
      printer_id: printerId,
      filament_plan: planChanged ? plan : [],
      nozzles,
      tier,
      process_name: processName,
    }
    const before = {
      printer_id: last?.printer_id ?? null,
      filament_plan: last?.filament_plan ?? [],
      nozzles: last?.nozzles ?? [],
      tier: last?.tier ?? null,
      process_name: last?.process_name ?? null,
    }
    if (JSON.stringify(next) === JSON.stringify(before)) return
    void api.putModelChoices(slug, next).catch(() => undefined)
  }

  /** #83 — the plate this printer now has on it, the fallback when it has no archives. */
  function rememberBedType() {
    if (printerId === null || bedType === null) return
    void api.putPrinterBedType(printerId, bedType).catch(() => undefined)
  }

  async function run() {
    if (!outputId || !choices || bedType === null) return
    const attempt = ++runAttempt.current
    setRunning(true)
    setRunError(null)
    setRefused(false)
    try {
      const body: PrintRunRequest = {
        printer_id: printerId,
        filament_plan: { slots: plan, force_colour_match: false },
        choices: {
          nozzles,
          tier,
          process_name: processName,
          bed_type: bedType,
          filament_overrides: overrides,
        },
        ...(copies === null ? {} : { copies }),
        plate_id: chosenPlate,
        all_plates: plate === 'all',
        project_id: projectId,
        options,
      }
      const ran = await api.runPrint(outputId, body)
      if (attempt !== runAttempt.current) return
      setResult(ran)
      onRan(ran)
      rememberChoices()
      rememberBedType()
    } catch (cause) {
      if (attempt !== runAttempt.current) return
      if (mayHaveRun(cause)) {
        setUnanswered((cause as ApiError).detail)
        return
      }
      setRunError(cause instanceof ApiError ? cause.detail : 'The print could not be started.')
      // Anything else (a refusal, nothing upstream took it) is worth retrying as it stands.
      setRefused(cause instanceof ApiError && cause.status === 422)
    } finally {
      if (attempt === runAttempt.current) setRunning(false)
    }
  }

  const presetsForSize = choices?.filament_presets?.[size] ?? []

  return (
    <Dialog
      open={open}
      title="Print"
      description={
        result || unanswered !== null
          ? undefined
          : 'Choose the spools, nozzles, quality and plate. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
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
        ) : unanswered !== null ? (
          <>
            <Button onClick={close}>Close</Button>
            {bambuddyUrl ? (
              <Button variant="primary" onClick={() => openExternal(`${bambuddyUrl}/queue`)}>
                {"Open Bambuddy's queue"}
              </Button>
            ) : (
              settings.error && (
                <Button onClick={settings.reload}>{"Find Bambuddy's queue"}</Button>
              )
            )}
          </>
        ) : (
          <>
            <Button onClick={close} disabled={running}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void run()}
              disabled={running || loading || !choices || refused}
              data-testid="run-print"
              {...USER_ONLY}
            >
              {running && <Spinner />}
              Print
            </Button>
          </>
        )
      }
    >
      {result ? (
        <div className="space-y-2 text-[13px] text-ink">
          <div data-testid="queued-items">
            <p>
              Sliced and queued for {printer?.name ?? 'the printer you chose'} —{' '}
              <span className="sb-num">{result.copies}</span>{' '}
              {result.copies === 1 ? 'copy' : 'copies'} in{' '}
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
          <WarningList warnings={result.warnings ?? []} testId="run-warnings" />
          <PrintProgressPanel progress={progress} polling={polling} />
        </div>
      ) : unanswered !== null ? (
        <p role="alert" className="text-[13px] text-warn" data-testid="run-unanswered">
          {unanswered}{' '}
          {
            "The print may still have been queued. Check Bambuddy's queue before printing again, or it may print twice."
          }
        </p>
      ) : (
        <>
          {loading && !choices && (
            <p className="flex items-center gap-2 text-[13px] text-muted">
              <Spinner /> Reading the printer and the inventory
            </p>
          )}

          {loadError && (
            <p role="alert" className="text-[13px] text-warn">
              {loadError}
            </p>
          )}

          {choices && (
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <span id="print-advanced" className="text-[13px] text-ink">
                  Advanced
                </span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={advanced}
                  aria-labelledby="print-advanced"
                  aria-describedby="print-advanced-help"
                  onClick={toggleAdvanced}
                  className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors ${
                    advanced ? 'border-accent bg-accent' : 'border-line-strong bg-surface-3'
                  }`}
                >
                  <span
                    className={`absolute top-[2px] size-3.5 rounded-full transition-[left] ${
                      advanced ? 'left-[18px] bg-accent-ink' : 'left-[2px] bg-muted'
                    }`}
                  />
                </button>
                <span id="print-advanced-help" className="text-[12px] text-faint">
                  Pick any process, the flow per side and a preset per slot.
                </span>
              </div>
              {printers.length > 1 && (
                <div>
                  <label htmlFor="print-printer" className="block text-[13px]">
                    Printer
                  </label>
                  <select
                    id="print-printer"
                    value={printerId === null ? '' : String(printerId)}
                    onChange={(event) =>
                      setAskedPrinter(event.target.value === '' ? null : Number(event.target.value))
                    }
                    className="sb-field mt-1.5 cursor-pointer"
                  >
                    {printerId === null && <option value="">Choose a printer</option>}
                    {printers.map((entry) => (
                      <option key={entry.id} value={entry.id}>
                        {entry.name}
                        {entry.model ? ` (${entry.model})` : ''}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {filamentError && (
                <p className="text-[12px] text-warn" data-testid="filaments-unavailable">
                  ScadBuddy could not read the filament inventory: {filamentError}
                </p>
              )}
              {filaments && (
                <FilamentPicker
                  options={filaments}
                  plan={plan}
                  onChange={setPlan}
                  copies={effectiveCopies}
                />
              )}
              {(target?.manifest ?? []).length > 0 && (
                <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
                  <legend className="px-1 text-[13px] text-ink">Arrange</legend>
                  <label htmlFor="arrange-for" className="text-[12px] text-muted">
                    Arrange for
                  </label>
                  <select
                    id="arrange-for"
                    value={arrangeGoal}
                    onChange={(event) => setArrangeGoal(event.target.value as ArrangeGoal)}
                    className="sb-field mt-1.5"
                  >
                    {Object.entries(GOAL_LABELS)
                      .filter(([value]) => value !== 'keep_together')
                      .map(([value, text]) => (
                        <option key={value} value={value}>
                          {text}
                        </option>
                      ))}
                  </select>
                  <Button
                    size="sm"
                    className="mt-1.5"
                    disabled={arranging}
                    onClick={() => void rearrange()}
                  >
                    Re-arrange for these spools
                  </Button>
                  {arrangeNote && (
                    <p aria-live="polite" className="mt-1.5 text-[12px] text-muted">
                      {arrangeNote}
                    </p>
                  )}
                  {arrangeError && (
                    <p role="alert" className="mt-1.5 text-[13px] text-warn">
                      {arrangeError}
                    </p>
                  )}
                </fieldset>
              )}

              {advanced && filaments && (filaments.slots ?? []).length > 0 && (
                <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
                  <legend className="px-1 text-[13px] text-ink">
                    Filament presets — {size} mm nozzle
                  </legend>
                  <div className="mt-1.5 flex flex-col gap-2">
                    {(filaments.slots ?? []).map((slot) => {
                      const id = `preset-override-${slot.slot_id}`
                      const chosen = overrides[String(slot.slot_id)]
                      return (
                        <div key={slot.slot_id} className="flex flex-col gap-1">
                          <label htmlFor={id} className="text-[12px] text-muted">
                            Preset for slot {slot.slot_id}
                          </label>
                          <select
                            id={id}
                            value={chosen ? refKey(chosen) : ''}
                            onChange={(event) => setOverride(slot.slot_id, event.target.value)}
                            className="sb-field"
                          >
                            <option value="">The spool&apos;s own preset</option>
                            {presetsForSize.map((row) => (
                              <option key={refKey(row.ref)} value={refKey(row.ref)}>
                                {row.name}
                              </option>
                            ))}
                          </select>
                        </div>
                      )
                    })}
                  </div>
                </fieldset>
              )}

              <NozzleStep
                sizes={choices.nozzle_sizes ?? []}
                installed={choices.installed ?? []}
                advanced={advanced}
                value={nozzles}
                onChange={changeNozzles}
              />
              <QualityStep
                size={size}
                tiers={choices.tiers?.[size] ?? []}
                processes={choices.processes?.[size] ?? []}
                advanced={advanced}
                tier={tier}
                processName={processName}
                onChange={(next) => {
                  setTier(next.tier)
                  setProcessName(next.processName)
                }}
              />
              {bedType !== null && (
                <PlateStep
                  bedTypes={
                    (choices.bed_types ?? []).includes(bedType)
                      ? (choices.bed_types ?? [])
                      : [bedType, ...(choices.bed_types ?? [])]
                  }
                  value={bedType}
                  lastBedType={choices.last_bed_type ?? null}
                  printerName={printer?.name ?? null}
                  onChange={setBedType}
                />
              )}

              {plates.length > 1 && outputId && (
                <fieldset data-testid="plate-choice">
                  <legend className="text-[13px]">Plates to print</legend>
                  <div className="mt-1.5 flex flex-wrap gap-2">
                    {plates.map((entry) => (
                      <label
                        key={entry.index}
                        className={`flex cursor-pointer items-center gap-2 rounded-[6px] border p-2 text-[13px] ${
                          plate === entry.index
                            ? 'border-accent bg-accent/8'
                            : 'border-line bg-surface-2'
                        }`}
                      >
                        <input
                          type="radio"
                          name="print-plate"
                          checked={plate === entry.index}
                          onChange={() => setPlate(entry.index)}
                          className="accent-[var(--sb-accent)]"
                        />
                        {entry.has_thumbnail && (
                          <img
                            src={api.outputPlateThumbnailUrl(outputId, entry.index)}
                            alt={`Plate ${entry.index}`}
                            className="h-12 w-12 rounded-[4px] object-contain"
                          />
                        )}
                        Plate <span className="sb-num">{entry.index}</span>
                      </label>
                    ))}
                    <label
                      className={`flex cursor-pointer items-center gap-2 rounded-[6px] border p-2 text-[13px] ${
                        plate === 'all' ? 'border-accent bg-accent/8' : 'border-line bg-surface-2'
                      }`}
                    >
                      <input
                        type="radio"
                        name="print-plate"
                        checked={plate === 'all'}
                        onChange={() => setPlate('all')}
                        className="accent-[var(--sb-accent)]"
                      />
                      All plates
                    </label>
                  </div>
                  {plate === 'all' && (
                    <p className="mt-1.5 text-[12px] text-faint">One queue item per plate.</p>
                  )}
                </fieldset>
              )}

              <PrintOptionsDisclosure
                slug={slug}
                printerId={printerId}
                value={copies === null ? options : { ...options, quantity: copies }}
                onChange={({ quantity, ...rest }) => {
                  setCopies(quantity ?? null)
                  setOptions(rest)
                }}
              />

              {/* #79 — a send to a project uploads into that project's folder. */}
              <ProjectPicker value={projectId} onChange={setProjectId} onLoaded={setProjectId} />

              <div className="flex items-center gap-3">
                <label htmlFor="print-copies" className="text-[13px] text-ink">
                  Copies
                </label>
                <input
                  id="print-copies"
                  type="number"
                  min={1}
                  max={MAX_COPIES}
                  value={copies ?? ''}
                  placeholder={String(rememberedCopies ?? 1)}
                  onChange={(event) =>
                    setCopies(
                      event.target.value === '' ? null : Math.max(1, Number(event.target.value)),
                    )
                  }
                  className="sb-field sb-num w-20 text-right"
                />
                {copies === null && rememberedCopies !== null && (
                  <span className="text-[12px] text-muted" data-testid="remembered-copies">
                    <span className="sb-num">{rememberedCopies}</span> remembered — leave blank
                    to use it
                  </span>
                )}
              </div>

            </div>
          )}

          {runError && (
            <p role="alert" className="mt-3 text-[13px] text-warn">
              {runError}
            </p>
          )}
        </>
      )}
    </Dialog>
  )
}
