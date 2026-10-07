import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError, nextRackAlgorithmVersion, rackAlgorithmSave } from '../api/client'
import type {
  AnalysisRequest,
  FilamentWarning,
  Output,
  PrintOptions,
  PrintOptionsState,
  PrintRunRequest,
  PrintRunResult,
  RackAlgorithm,
} from '../api/types'
import {
  arrangedName,
  arrangedNote,
  backfillFailures,
  backfillIds,
  backfillNote,
  backfillOutputs,
  GOAL_LABELS,
  needsBackfill,
  runArrange,
  type ArrangeGoal,
} from '../lib/arrange'
import { BackfillProgress, BackfillPrompt } from './BackfillPrompt'
import { openExternal } from '../lib/embed'
import { printChoicesOf } from '../lib/printChoices'
import { resolveOptions } from '../lib/printOptions'
import { sourceApi, sourceKey, type PrintSource } from '../lib/printSource'
import { useAsync } from '../lib/useAsync'
import { CarryBox, useFilamentPlan } from '../lib/useFilamentPlan'
import { usePrintCheck } from '../lib/usePrintCheck'
import { usePrintChoices } from '../lib/usePrintChoices'
import { usePrintProgress } from '../lib/usePrintProgress'
import { useRunPrint } from '../lib/useRunPrint'
import { FilamentPicker } from './FilamentPicker'
import { AdvancedSwitch } from './print/AdvancedSwitch'
import { AnalyzerPanel } from './print/AnalyzerPanel'
import { CopiesField } from './print/CopiesField'
import { NozzleStep } from './print/NozzleStep'
import { PrintVerdict } from './print/PrintVerdict'
import { PlatesToPrint } from './print/PlatesToPrint'
import { PlateStep } from './print/PlateStep'
import { PresetOverrides } from './print/PresetOverrides'
import { QualityStep } from './print/QualityStep'
import { QueuedPanel } from './print/QueuedPanel'
import { RackNozzleLine, RackNozzleStep } from './print/RackNozzle'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'
import { type ProjectList, useProjectList } from '../lib/projects'
import { ProjectPicker } from './ProjectPicker'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'
import { bambuddyBase, bambuddyLink, webUrls } from '../lib/bambuddyLinks'

/**
 * The print dialog, spool-first (docs/superpowers/specs/2026-09-27-spool-first-print-design.md).
 *
 * You choose what you actually think about — the spools from the inventory, the nozzle
 * size, a quality tier and the plate — and ScadBuddy derives every Bambu preset from
 * those choices server-side, then slices and queues through Bambuddy. There are no
 * slicer pipelines here any more; they stay in Bambuddy untouched.
 *
 * - One read, the source's choices read (`/print/outputs/{id}/…` or
 *   `/print/library/{file_id}/…`, #313), opens the dialog: printers, installed
 *   nozzles, tiers and processes per size, plate types with the one last printed on,
 *   the filament step, and what this model last printed with (#78).
 * - One write, the source's run (`/print/outputs/{id}/…` or `/print/library/{file_id}/…`,
 *   #313), prints. A 422 is the resolver refusing a
 *   combination (an unpicked slot, no process, no preset for a spool at this size); its
 *   `detail` is shown above Print and the dialog stays open.
 * - Simple mode shows only what the user has to choose (#768): the printer, when there is
 *   more than one, the spools, which plate of a multi-plate file, and Print, with the
 *   Checks. The nozzles, quality, plate type, print options, project and copies are
 *   Advanced steps, and Simple sends what they open on: the size and tier this model
 *   last printed with (else 0.4 mm, Standard), the plate type the choices read chose, the
 *   remembered copies and the page's project, else the last one printed to. Advanced also adds the full process list,
 *   per-side flow and a per-slot filament preset override.
 *
 * Around that it keeps what the send bar's print already had: the options disclosure
 * (#88), the project (#79), copies with the remembered quantity (#124/#145), which plate
 * of a multi-plate 3MF (#83), and following the run to completion (#89).
 */

/**
 * The notes about the nozzle step, shown in Advanced mode only (#772): the rack's
 * `not-installed`. A side the slice may use with a nozzle of the chosen size mounted in
 * the other flow (`hf-mounted`, #797, #484) is not one of them, so Simple shows it.
 */
const NOZZLE_WARNINGS: ReadonlySet<FilamentWarning['kind']> = new Set(['not-installed'])

interface Props {
  open: boolean
  /** #313 — an output ScadBuddy rendered, or a file in Bambuddy's library. */
  source: PrintSource | undefined
  onClose: () => void
  onRan: (result: PrintRunResult) => void
  /** #81 — the model of the printer in view, so the preview can draw its plate. */
  onPrinterModel?: (model: string | null) => void
  /**
   * #317 — the project chosen on the Customize page. Given, the dialog's picker shows and
   * moves that one choice rather than a copy of its own, so the two never disagree.
   */
  project?: {
    value: number | null
    onChange: (projectId: number | null) => void
    /** The page's project list, so the dialog does not fetch it a second time. */
    list?: ProjectList
    /** #665 — frozen while the page's Generate is filing into the chosen project. */
    disabled?: boolean
    /** #665 — a "Create project" in this picker is in flight. */
    onCreating?: (creating: boolean) => void
  }
}

export function PrintPicker({ open, source: given, onClose, onRan, onPrinterModel, project }: Props) {
  /** §7 — the output a Re-arrange made of the one passed in, printed in its place. */
  const [arranged, setArranged] = useState<Output | null>(null)
  const [arrangeGoal, setArrangeGoal] = useState<ArrangeGoal>('fewest_swaps')
  const [arranging, setArranging] = useState(false)
  const [arrangeNote, setArrangeNote] = useState<string | null>(null)
  const [arrangeError, setArrangeError] = useState<string | null>(null)
  /** #902 — the output in view, read back after a re-render gave it its objects. */
  const [backfilled, setBackfilled] = useState<Output | null>(null)
  /** The output being asked about (re-render it first?), by id, so it never names another. */
  const [askFor, setAskFor] = useState<string | null>(null)
  /** The output Arrange said needs a re-render, though it showed objects; by id, likewise. */
  const [flaggedId, setFlaggedId] = useState<string | null>(null)
  const [backfillProgress, setBackfillProgress] = useState<Record<string, string>>({})
  /**
   * What a re-arrange was made for, carried across the switch to its output: the new
   * file's slot N is the old one's colour (`colours` pins the order), so the plan and
   * the settings still apply. While it is set, the hooks keep the dialog's choices
   * instead of resetting and seeding them for a new source.
   */
  const [carry] = useState(() => new CarryBox())
  /** The re-arrange in flight: closing the dialog (or unmounting it) stops its wait. */
  const arrangeRun = useRef<AbortController | null>(null)
  useEffect(() => {
    if (open) return
    arrangeRun.current?.abort()
    arrangeRun.current = null
    setAskFor(null)
    setFlaggedId(null)
  }, [open])
  useEffect(() => () => arrangeRun.current?.abort(), [])
  const givenKey = sourceKey(given)
  useEffect(() => {
    // A new source from the caller is not a re-arrange: nothing carries to it, and a
    // re-arrange of the old one still in flight must not land on it.
    arrangeRun.current?.abort()
    arrangeRun.current = null
    carry.set(null)
    setArranged(null)
    setAskFor(null)
    setFlaggedId(null)
  }, [givenKey, carry])
  const source: PrintSource | undefined = arranged ? { kind: 'output', output: arranged } : given
  /** The output in view, with what Re-arrange needs of it (a library file has none). */
  const inView = source?.kind === 'output' ? source.output : undefined
  const target = inView && backfilled?.id === inView.id ? backfilled : inView
  /** Its objects, when the caller knows them; an unknown list offers no Re-arrange. */
  const known = target?.manifest !== undefined
  const stale = known && (flaggedId === target.id || needsBackfill(target))
  const askBackfill = target !== undefined && askFor === target.id

  /**
   * The model, for its print-options scope — the same slug its choices are remembered
   * under (`sourceApi`). A library file has none.
   */
  const slug = source?.kind === 'output' ? source.output.slug : undefined
  // A library run polls nothing and attaches nothing: its progress is Bambuddy's queue (#313).
  const outputId = source?.kind === 'output' ? source.output.id : undefined
  const picker = usePrintChoices(open, source, carry)
  const { choices, choicesRead, loading, loadError, printers, printerId, printer, selection, size } =
    picker
  const { nozzles, tier, processName, bedType, overrides, plate } = selection
  const { filaments, plan, setPlan, planChanged, filamentError } = useFilamentPlan(
    source,
    choices,
    plate,
    carry,
  )

  // null until the user sets it, so a remembered quantity is not overridden by the
  // box's own starting value (#124).
  const [copies, setCopies] = useState<number | null>(null)
  /** #145 — the remembered options, so the box can say what an unset Copies queues. */
  const [remembered, setRemembered] = useState<PrintOptionsState | null>(null)
  /** #88 — this print's overrides, all but `quantity`, which is `copies`. */
  const [options, setOptions] = useState<PrintOptions>({})
  /**
   * #836 — the rack's ranking for this print (remembered per printer, so it opens on what
   * the choices read says) and a hand-picked position, `null` for Automatic. A hand pick
   * names one hotend for one printer and one nozzle size, so either changing drops it.
   */
  /**
   * Only an algorithm chosen in this dialog is sent; otherwise `null`, and the backend
   * applies the printer's remembered one, so one printer's can never go out for another
   * while the new printer's choices are read (claude-review on #1043).
   */
  const [chosenAlgorithm, setChosenAlgorithm] = useState<RackAlgorithm | null>(null)
  /**
   * A printer's algorithm as an earlier session's save left it, when that save landed
   * after this dialog started reading the choices (#1086 review). Shown only, never sent,
   * and only on that printer. A choices read started after the save landed supersedes it;
   * one started before it may still answer with the old algorithm, so it does not.
   */
  const [savedAlgorithm, setSavedAlgorithm] = useState<{
    printerId: number
    algorithm: RackAlgorithm
    /** The last choices read started when the save landed. */
    afterRead: number
    /** The save's `algorithmSave` number, so an earlier one landing later is ignored. */
    save: number
  } | null>(null)
  const storedAlgorithm =
    savedAlgorithm && savedAlgorithm.printerId === printerId && choicesRead <= savedAlgorithm.afterRead
      ? savedAlgorithm.algorithm
      : choices?.rack_algorithm
  const rackAlgorithm: RackAlgorithm = chosenAlgorithm ?? storedAlgorithm ?? 'least_used'
  const [rackPosition, setRackPosition] = useState<number | null>(null)
  /** A failed save of the algorithm: this print still uses it, the next one may not. */
  const [algorithmUnsaved, setAlgorithmUnsaved] = useState(false)
  /** Bumped when a session's algorithm is dropped, so a save still in flight cannot
   *  report its failure on the next one (#1086 review). */
  const algorithmSession = useRef(0)
  /** Bumped by each algorithm save: only the latest one's outcome counts, whatever
   *  order the answers arrive in (#1086 review). */
  const algorithmSave = useRef(0)
  /** The saves, one at a time: two in flight at once could be applied in either order,
   *  leaving the printer on a choice the user had replaced (#1086 review). */
  const algorithmSaves = useRef<Promise<unknown>>(Promise.resolve())
  // Only a change of printer drops the hand pick and the chosen algorithm.
  useEffect(() => {
    algorithmSession.current += 1
    setChosenAlgorithm(null)
    setRackPosition(null)
    setAlgorithmUnsaved(false)
  }, [printerId])
  // Simple mode shows no rack step, so a hand pick would be sent unseen (as
  // usePrintChoices' toggleAdvanced drops the other Advanced-only choices).
  const { advanced } = picker
  useEffect(() => {
    if (!advanced) setRackPosition(null)
  }, [advanced])
  useEffect(() => {
    setRackPosition(null)
  }, [size])
  function changeRackAlgorithm(next: RackAlgorithm) {
    setChosenAlgorithm(next)
    setAlgorithmUnsaved(false)
    const session = algorithmSession.current
    const save = ++algorithmSave.current
    const savedOn = printerId
    if (savedOn === null) return
    // Taken when chosen, not when sent, so the order is the user's (#1216).
    const version = nextRackAlgorithmVersion()
    const saving = algorithmSaves.current.then(() =>
      api.putPrinterRackAlgorithm(savedOn, next, version, AbortSignal.timeout(rackAlgorithmSave.timeoutMs)),
    )
    algorithmSaves.current = saving.catch(() => undefined)
    void saving.then(
      () => {
        // Saved after its session ended (#1086 review): the printer now stores `next`,
        // which a dialog that already read its choices does not know. Show it for that
        // printer without carrying it in as a choice, and without re-reading the
        // choices, which would reset the bed type and filament plan set since.
        if (algorithmSession.current !== session)
          setSavedAlgorithm((shown) =>
            shown && shown.printerId === savedOn && shown.save > save
              ? shown
              : { printerId: savedOn, algorithm: next, afterRead: picker.readsStarted.current, save },
          )
      },
      (error: unknown) => {
        // A 409 is a newer save already stored (#1216): by definition that one won, so
        // there is nothing to report.
        if (error instanceof ApiError && error.status === 409) return
        if (algorithmSave.current === save && algorithmSession.current === session)
          setAlgorithmUnsaved(true)
      },
    )
  }
  /** #79 — the Bambuddy project this print is filed under: the page's, when it has one. */
  const [ownProjectId, setOwnProjectId] = useState<number | null>(null)
  const projectId = project ? project.value : ownProjectId
  /**
   * #768 — the dialog's own list, read here rather than by its picker, which is an
   * Advanced step: Simple mode must still seed the project from the last one printed to.
   * Read on each open, as the picker was, and never when the page passes its project.
   */
  const ownProjects = useProjectList(setOwnProjectId, open && !project)

  /**
   * #710 review — a "Create project" in this dialog's own picker, tracked here (not just
   * reported up to `project.onCreating`) so `close()` can refuse to unmount the picker
   * while its request is in flight. Unmounting mid-create would drop the guard the parent
   * relies on without stopping the request, which still lands and moves the shared project
   * once nothing is on screen to notice.
   */
  const [projectCreating, setProjectCreating] = useState(false)
  function reportProjectCreating(creating: boolean) {
    setProjectCreating(creating)
    project?.onCreating?.(creating)
  }

  const runPrint = useRunPrint({
    source,
    choices,
    printerId,
    selection,
    plan,
    planChanged,
    copies,
    projectId,
    options,
    rackPosition,
    rackAlgorithm: chosenAlgorithm,
    onRan,
  })
  const { run, running, runError, refused, result, unanswered } = runPrint
  // Only for the queue link while a run is unanswered (a result carries its own), so it
  // is read then, not on every open; a failed read offers to try again.
  const settings = useAsync(
    async () => (open && unanswered !== null ? await api.getSettings() : null),
    [open, unanswered !== null],
  )
  const bambuddyUrl = bambuddyBase(webUrls(settings.data))

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

  // One output's options do not survive a change of output — the other half of
  // usePrintChoices' reset on the same `sourceKey` — except to the output a re-arrange
  // made of it, which keeps the dialog's settings.
  const { sourceKey: currentKey } = picker
  useEffect(() => {
    if (carry.get()) return
    setOptions({})
  }, [currentKey, carry])

  /** Re-arrange the output in view; `backfill` (confirmed by the user) re-renders it first. */
  async function rearrange(backfill: boolean) {
    if (!target?.slug) return
    setAskFor(null)
    setArranging(true)
    setArrangeNote(null)
    setArrangeError(null)
    const controller = new AbortController()
    arrangeRun.current = controller
    try {
      let from = target
      if (backfill) {
        const { ready, failed } = await backfillOutputs([target], {
          signal: controller.signal,
          onProgress: (output, message) => setBackfillProgress({ [output.id]: message }),
        })
        const read = ready[0]
        if (!read) {
          setArrangeError(backfillFailures(failed))
          return
        }
        from = read
        setBackfilled(read)
        setFlaggedId(null)
      }
      const next = await runArrange(
        from.slug,
        {
          objects: (from.manifest ?? []).map((object) => ({
            output_id: from.id,
            part: object.part,
            count: object.count,
          })),
          goal: arrangeGoal,
          printer_id: printerId,
          filament_plan: { slots: plan, force_colour_match: false },
          colours: from.colors ?? [],
          name: arrangedName(from.name),
        },
        { signal: controller.signal },
      )
      // Closed while the output was being saved: it is saved, a normal output in
      // History, but this dialog keeps the output it was opened for.
      if (controller.signal.aborted) return
      carry.set({ plan, ready: false })
      setArranged(next.output)
      setArrangeNote(arrangedNote(next.plates))
    } catch (cause) {
      if (controller.signal.aborted) return
      if (backfillIds(cause)) {
        setFlaggedId(target.id)
        setAskFor(target.id)
        return
      }
      setArrangeError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
    } finally {
      if (arrangeRun.current === controller) arrangeRun.current = null
      setArranging(false)
      setBackfillProgress({})
    }
  }

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

  const rememberedCopies =
    resolveOptions(
      remembered?.global_options,
      printerId === null ? undefined : remembered?.printers?.[String(printerId)],
      slug === undefined ? undefined : remembered?.models?.[slug],
    ).quantity ?? null
  const effectiveCopies = copies ?? rememberedCopies ?? 1

  /**
   * #284 — what the analyzers judge: this dialog's run request as `AnalysisRequest` takes
   * it (`backend/scadbuddy/analyzers/context.py:46`). It has no project. With
   * "All plates" the filament checks read every plate; the mesh checks read plate 1.
   */
  const printChoices = printChoicesOf(selection)
  const allPlates = plate === 'all'
  const analysisRequest: AnalysisRequest | null =
    choices && printChoices
      ? {
          printer_id: printerId,
          filament_plan: { slots: plan, force_colour_match: false },
          choices: printChoices,
          plate_id: allPlates ? 1 : plate,
          all_plates: allPlates,
          copies: effectiveCopies,
          options,
        }
      : null

  /**
   * #755, #760 — what the run would refuse for these choices, before Print: the same
   * refusals the run makes before upload. An error holds Print, since the run would 422;
   * one for choices since changed does not.
   */
  const checkRequest: PrintRunRequest | null =
    choices && printChoices
      ? {
          printer_id: printerId,
          filament_plan: { slots: plan, force_colour_match: false },
          choices: printChoices,
          plate_id: allPlates ? 1 : plate,
          all_plates: allPlates,
          rack_position: rackPosition,
          rack_algorithm: chosenAlgorithm,
        }
      : null
  const check = usePrintCheck(source, checkRequest)
  // #836 — a hand pick the current check no longer offers (no rack this time, or the
  // position gone from its options) would be sent unseen, so it goes back to Automatic.
  // Only a current verdict decides: one still on its way keeps the pick.
  const offered = check.current ? (check.verdict?.rack?.options ?? []) : null
  const pickOffered = rackPosition === null || offered === null || offered.some((o) => o.position === rackPosition)
  useEffect(() => {
    if (!pickOffered) setRackPosition(null)
  }, [pickOffered])
  const runRefuses = check.current && (check.verdict?.errors ?? []).length > 0
  /**
   * #772 — Simple mode hides the notes about the nozzle step, which only Advanced shows.
   * The mounted High Flow warning (#723, #797) is shown in both, and never holds Print.
   */
  const shown = (warnings: FilamentWarning[] | undefined) =>
    picker.advanced
      ? (warnings ?? [])
      : (warnings ?? []).filter((warning) => !NOZZLE_WARNINGS.has(warning.kind))
  const checkVerdict = check.verdict && { ...check.verdict, warnings: shown(check.verdict.warnings) }
  const verdict = (
    <>
      <RackNozzleLine rack={check.verdict?.rack} algorithm={rackAlgorithm} />
      <PrintVerdict verdict={checkVerdict} error={check.error} onRetry={check.reload} />
    </>
  )
  const verdictShown =
    check.error !== undefined ||
    Boolean(check.verdict?.rack) ||
    (checkVerdict?.errors ?? []).length + (checkVerdict?.warnings ?? []).length > 0

  function close() {
    // Escape and the backdrop are ignored mid-run, as Cancel is: a closed dialog would
    // reopen with Print enabled and send the print a second time (#539 review).
    // Also ignored mid-create (#710 review): unmounting the picker would drop the
    // creating guard while its request is still in flight.
    if (running || projectCreating) return
    // The page's project (#317) outlives the dialog; only its own copy is reset.
    setOwnProjectId(null)
    setOptions({})
    setRackPosition(null)
    // #1084: the algorithm chosen here, and a failed save of it, are this session's too.
    algorithmSession.current += 1
    setChosenAlgorithm(null)
    setAlgorithmUnsaved(false)
    runPrint.reset()
    picker.reset()
    // The choices it carried are reset with the rest.
    carry.set(null)
    onClose()
  }

  return (
    <Dialog
      open={open}
      title="Print"
      description={
        result || unanswered !== null
          ? undefined
          : picker.advanced
            ? 'Choose the spools, nozzles, quality and plate. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
            : 'Choose the spools. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
      }
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            {result.bambuddy_url && (
              <Button variant="primary" onClick={() => openExternal(bambuddyLink(result.bambuddy_url as string))}>
                Open in queue
              </Button>
            )}
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
            <Button onClick={close} disabled={running || projectCreating}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void run()}
              // Held while the dialog's own project list loads, so a Simple-mode print
              // cannot go out before the last project has seeded it.
              disabled={
                running || loading || !choices || refused || runRefuses || ownProjects.loading || projectCreating
              }
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
        <QueuedPanel
          result={{ ...result, warnings: shown(result.warnings) }}
          printerName={printer?.name ?? null}
          progress={progress}
          polling={polling}
        />
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
            <div className="flex items-baseline gap-3">
              <p role="alert" className="text-[13px] text-warn">
                {loadError}
              </p>
              {/* #482: a Bambuddy blip or timeout should not need the dialog reopened. */}
              <button
                type="button"
                onClick={picker.reload}
                className="text-[12px] text-muted underline decoration-dotted underline-offset-2 hover:text-ink"
              >
                Retry
              </button>
            </div>
          )}

          {choices && (
            <div className="space-y-3">
              <AdvancedSwitch
                value={picker.advanced}
                onToggle={picker.toggleAdvanced}
                disabled={projectCreating}
              />
              {printers.length > 1 && (
                <div>
                  <label htmlFor="print-printer" className="block text-[13px]">
                    Printer
                  </label>
                  <select
                    id="print-printer"
                    value={printerId === null ? '' : String(printerId)}
                    onChange={(event) =>
                      picker.askPrinter(
                        event.target.value === '' ? null : Number(event.target.value),
                      )
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
              {target && known && (
                <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
                  <legend className="px-1 text-[13px] text-ink">Arrange</legend>
                  {stale && <p className="mb-1.5 text-[12px] text-muted">{backfillNote([target])}</p>}
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
                    disabled={arranging || askBackfill}
                    onClick={() => (stale ? setAskFor(target.id) : void rearrange(false))}
                  >
                    Re-arrange for these spools
                  </Button>
                  {askBackfill && (
                    <div className="mt-1.5">
                      <BackfillPrompt
                        outputs={[target]}
                        onConfirm={() => void rearrange(true)}
                        onCancel={() => setAskFor(null)}
                      />
                    </div>
                  )}
                  <BackfillProgress outputs={[target]} progress={backfillProgress} />
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

              {picker.advanced && filaments && (filaments.slots ?? []).length > 0 && (
                <PresetOverrides
                  size={size}
                  slots={filaments.slots ?? []}
                  presets={choices.filament_presets?.[size] ?? []}
                  overrides={overrides}
                  onChange={picker.setOverride}
                />
              )}

              {picker.plates.length > 1 && source && (
                <PlatesToPrint
                  plates={picker.plates}
                  value={plate}
                  onChange={picker.setPlate}
                  thumbnailUrl={sourceApi(source).plateThumbnailUrl}
                />
              )}

              {/* #768 — Simple mode sends these steps' defaults without showing them. */}
              {picker.advanced && (
                <>
                  <NozzleStep
                    sizes={choices.nozzle_sizes ?? []}
                    installed={choices.installed ?? []}
                    value={nozzles}
                    onChange={picker.changeNozzles}
                  />
                  {check.verdict?.rack && (
                    <RackNozzleStep
                      rack={check.verdict.rack}
                      algorithm={rackAlgorithm}
                      position={rackPosition}
                      algorithmUnsaved={algorithmUnsaved}
                      onAlgorithm={changeRackAlgorithm}
                      onPosition={setRackPosition}
                    />
                  )}
                  <QualityStep
                    size={size}
                    tiers={choices.tiers?.[size] ?? []}
                    processes={choices.processes?.[size] ?? []}
                    tier={tier}
                    processName={processName}
                    onChange={picker.changeQuality}
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
                      onChange={picker.setBedType}
                    />
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
                  {project ? (
                    <ProjectPicker
                      value={project.value}
                      onChange={project.onChange}
                      list={project.list}
                      disabled={project.disabled || projectCreating}
                      onCreating={reportProjectCreating}
                    />
                  ) : (
                    <ProjectPicker
                      value={ownProjectId}
                      onChange={setOwnProjectId}
                      list={ownProjects}
                      disabled={projectCreating}
                      onCreating={reportProjectCreating}
                    />
                  )}

                  <CopiesField value={copies} remembered={rememberedCopies} onChange={setCopies} />
                </>
              )}

              {/* The analyzers judge an output's own 3MF; a library file has none (#313), so its
                  Checks are the nozzle verdict alone (#755). */}
              {outputId !== undefined ? (
                <AnalyzerPanel outputId={outputId} request={analysisRequest} allPlates={allPlates}>
                  {verdict}
                </AnalyzerPanel>
              ) : (
                verdictShown && (
                  <section
                    aria-labelledby="print-checks-title"
                    data-testid="print-checks"
                    className="rounded-[6px] border border-line bg-surface-2 px-3 py-2"
                  >
                    <h3 id="print-checks-title" className="text-[13px] text-ink">
                      Checks
                    </h3>
                    {verdict}
                  </section>
                )
              )}
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
