import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError, nextRackAlgorithmVersion, rackAlgorithmSave } from '../api/client'
import type {
  AnalysisRequest,
  AnalysisTarget,
  BatchSummary,
  FilamentOptions,
  FilamentWarning,
  NozzlePlan as NozzlePlanView,
  Output,
  PrintOptions,
  PrintOptionsState,
  PrintRunRequest,
  PrintRunResult,
  PrintSequence,
  SupportsChoice,
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
import { traceAction } from '../lib/traceAction'
import { BackfillProgress, BackfillPrompt } from './BackfillPrompt'
import { openExternal } from '../lib/embed'
import { plateLabel } from '../lib/plate'
import { resolvedColors } from '../lib/plateColors'
import { checkPlan, slicedPlateSlots, withSlicedGrams } from '../lib/filaments'
import { useFilamentProfiles } from '../lib/filamentProfiles'
import { printChoicesOf, sliceOverrides } from '../lib/printChoices'
import { resolveOptions } from '../lib/printOptions'
import {
  attachPrintToProject,
  optionsSubject,
  printSubject,
  sourceApi,
  sourceKey,
  type PrintSource,
} from '../lib/printSource'
import { useAsync } from '../lib/useAsync'
import { CarryBox, useFilamentPlan } from '../lib/useFilamentPlan'
import { usePrintCheck } from '../lib/usePrintCheck'
import { usePreviewSlice } from '../lib/usePreviewSlice'
import { asSliced } from '../lib/slicedSides'
import { usePrintChoices } from '../lib/usePrintChoices'
import { usePrintProgress } from '../lib/usePrintProgress'
import { printRunBody, useRunPrint } from '../lib/useRunPrint'
import { FilamentPicker } from './FilamentPicker'
import { AdvancedSwitch } from './print/AdvancedSwitch'
import { AnalyzerPanel } from './print/AnalyzerPanel'
import { CopiesField } from './print/CopiesField'
import { FlowChoice } from './print/FlowChoice'
import { NozzlePlanLine, NozzlePlanStep } from './print/NozzlePlan'
import { NozzleStep } from './print/NozzleStep'
import { PreviewPane } from './print/PreviewPane'
import { TrayQuestion } from './print/TrayQuestion'
import { useDeclinedTrays } from '../lib/trays'
import { PrintVerdict } from './print/PrintVerdict'
import { PlatePreviewDialog } from './print/PlatePreviewDialog'
import { PlatesToPrint } from './print/PlatesToPrint'
import { PlateStep } from './print/PlateStep'
import { PresetOverrides } from './print/PresetOverrides'
import { FilamentProfilesStep } from './print/FilamentProfilesStep'
import { PrintSequenceStep } from './print/PrintSequenceStep'
import { SupportsStep } from './print/SupportsStep'
import { overriddenSupportKeys } from '../lib/supports'
import { SlicerDefaults } from './print/SlicerDefaults'
import { SlicedSettings } from './print/SlicedSettings'
import { slicedEstimate } from '../lib/slicedEstimate'
import { QualityStep } from './print/QualityStep'
import { QueuedPanel } from './print/QueuedPanel'
import { RackNozzleLine, RackNozzleStep } from './print/RackNozzle'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'
import { breadcrumbs, type ProjectList, useProjectList } from '../lib/projects'
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
 *   remembered copies and the page's project, else the last one printed to; that project is
 *   named in a "Filed under" line (#1660). Advanced also adds the full process list,
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

/**
 * #2672 — the dialog as one step of a batch print: Back, Skip and Next in place of Print,
 * the step strip above the choices, and the choices it opens on.
 */
export interface BatchStep {
  /** "File 2 of 5 — name". */
  title: string
  strip: ReactNode
  /**
   * What the step opens on: this file's own saved choices, or the last file's carried
   * forward. A carried printer always applies; the spools, nozzles, quality and options
   * only when this file's slots are the same as the one they were chosen for.
   */
  seed: PrintRunRequest | null
  /** `seed` is this file's own (a resume, or Edit from the review): applied whatever its slots. */
  seedIsOwn: boolean
  /** The slots a carried `seed` was chosen for, as `slotKeys` makes them. */
  seedSlots: string[]
  onBack: (() => void) | null
  onSkip: () => void
  onNext: (choice: { request: PrintRunRequest; summary: BatchSummary; problems: string[] }) => void
  /** The last file: Next goes to the review. */
  last: boolean
  /** A step's save in flight. */
  busy: boolean
}

/** #2672 — a file's slots, what decides whether the last file's spools carry to it. */
function slotKeys(filaments: FilamentOptions): string[] {
  return (filaments.slots ?? [])
    .map((slot) => `${slot.slot_id}:${slot.material ?? ''}:${(slot.colour ?? '').toUpperCase()}`)
    .sort()
}

interface Props {
  open: boolean
  /** #2672 — one step of a batch print, which never runs a print itself. */
  batch?: BatchStep
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
  /**
   * #1294 — the model's own slicer defaults (`print_settings`), shown read-only for an
   * output. A library file has none of ScadBuddy's.
   */
  printSettings?: Record<string, string> | undefined
}

export function PrintPicker({
  open,
  source: given,
  onClose,
  onRan,
  onPrinterModel,
  project,
  printSettings,
  batch,
}: Props) {
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
   * What its own print options are remembered under, as its choices are (#1754): the
   * model, or the library file. A string, so the effects below see a change of key.
   */
  const subject = optionsSubject(source)
  const scopeKey = subject?.key
  /** #1751 — what this dialog's print is followed and filed by: the output, or the library file. */
  const printed = printSubject(source)
  /** What the analyzers judge: the output, or the library file, as any print (#1753). */
  const analysisTarget: AnalysisTarget | undefined = !source
    ? undefined
    : source.kind === 'output'
      ? { output_id: source.output.id }
      : { library_file_id: source.file.id }
  const picker = usePrintChoices(open, source, carry)
  const { choices, choicesRead, loading, loadError, printers, printerId, printer, selection, size } =
    picker
  const { nozzles, tier, processName, bedType, overrides, plate } = selection
  const { filaments, plan, setPlan, planChanged, filamentError, planReady, retryFilaments } = useFilamentPlan(
    source,
    choices,
    plate,
    carry,
  )
  /**
   * #2671 — a sliced file: queued as it is, so its slicer choices are shown read-only
   * and only the plates it holds G-code for are offered.
   */
  const sliced = choices?.sliced ?? null
  const slicedPlates = useMemo(() => (sliced?.plates ?? []).map((entry) => entry.index), [sliced])
  const plates = useMemo(
    () =>
      slicedPlates.length > 0 ? picker.plates.filter((entry) => slicedPlates.includes(entry.index)) : picker.plates,
    [picker.plates, slicedPlates],
  )
  const { setPlate } = picker
  useEffect(() => {
    const first = slicedPlates[0]
    if (first !== undefined && plate !== 'all' && !slicedPlates.includes(plate)) setPlate(first)
  }, [slicedPlates, plate, setPlate])

  // null until the user sets it, so a remembered quantity is not overridden by the
  // box's own starting value (#124).
  const [copies, setCopies] = useState<number | null>(null)
  /** #1723 — the plate in 3D, over this dialog; every choice here stays as it is. */
  const [previewOpen, setPreviewOpen] = useState(false)
  const previewColors = useMemo(
    () => resolvedColors(filaments?.slots ?? [], filaments?.spools ?? [], plan),
    [filaments, plan],
  )
  /** #145 — the remembered options, so the box can say what an unset Copies queues. */
  const [remembered, setRemembered] = useState<PrintOptionsState | null>(null)
  /** #88 — this print's overrides, all but `quantity`, which is `copies`. */
  const [options, setOptions] = useState<PrintOptions>({})
  /**
   * #1862 — this print's sequence, `null` for the template's or the process's. Per print,
   * never remembered (until #1754 decides remembered choices by subject).
   */
  const [printSequence, setPrintSequence] = useState<PrintSequence | null>(null)
  // #1899 — this print's supports; `null` leaves them to the template and the process.
  const [supports, setSupports] = useState<SupportsChoice | null>(null)
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
  /**
   * #768 — the dialog's own list, read here rather than by its picker, which is an
   * Advanced step: Simple mode must still seed the project from the last one printed to.
   * Read on each open, as the picker was, and never when the page passes its project.
   */
  const ownProjects = useProjectList(setOwnProjectId, open && !project)
  /**
   * #1045 — while the list has not been read (still loading, or failed) an unset picker
   * is not a choice of "No project": the run leaves `project_id` out, so the server files
   * it under the remembered project instead.
   */
  const pickedProject = project ? project.value : ownProjectId
  // A page project without a shared list is read by its own picker, out of sight here.
  const projectKnown = project ? project.list?.choices !== null : ownProjects.choices !== null
  const projectId = pickedProject === null && !projectKnown ? undefined : pickedProject

  /**
   * #1660 — the project the print is filed under, named, for Simple mode: its picker is an
   * Advanced step, so a project carried over from earlier would otherwise go unseen. None
   * while the list is unread, when the server chooses (`projectId` undefined).
   */
  const shownProjects = (project ? project.list : ownProjects)?.choices?.projects
  const projectPaths = useMemo(() => breadcrumbs(shownProjects ?? []), [shownProjects])
  const filedUnderLabel =
    projectId === undefined ? null : projectId === null ? 'No project' : (projectPaths.get(projectId) ?? `project ${projectId}`)
  /** "(change)" opens Advanced, then puts the focus on the project step it shows. */
  const focusProject = useRef(false)
  useEffect(() => {
    if (!picker.advanced || !focusProject.current) return
    focusProject.current = false
    const select = document.getElementById('print-project')
    select?.scrollIntoView?.({ block: 'nearest' })
    select?.focus()
  }, [picker.advanced])

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

  // #717 — profiles the chosen spools match, each on unless switched off.
  const filamentProfiles = useFilamentProfiles({
    spools: filaments?.spools,
    plan,
    choices: printChoicesOf(selection),
  })

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
    printSequence,
    supports,
    filamentProfiles: filamentProfiles.active,
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
  const { progress, polling } = usePrintProgress(printed, open && result !== null)

  /**
   * #79 — file the finished print under its project, once the progress read (#89) has
   * the queue entries. Best effort, once per settled print, guarded by a ref so the
   * guard itself does not re-render and re-run the effect.
   */
  const attached = useRef<string | null>(null)
  useEffect(() => {
    // A run that left the project to the server says where it went (#1045).
    const filedUnder = projectId === undefined ? (result?.project_id ?? null) : projectId
    if (!printed || filedUnder === null || !progress?.settled) return
    const entries = (progress.copies_detail ?? [])
      .map((copy) => copy.queue_entry_id)
      .filter((id): id is number => typeof id === 'number')
    if (entries.length === 0) return
    const key = `${printed}:${filedUnder}:${entries.join(',')}`
    if (attached.current === key) return
    attached.current = key
    void attachPrintToProject(printed, { project_id: filedUnder, queue_item_ids: entries }).catch(() => {
      attached.current = null
    })
  }, [printed, projectId, result, progress])

  // One output's options do not survive a change of output — the other half of
  // usePrintChoices' reset on the same `sourceKey` — except to the output a re-arrange
  // made of it, which keeps the dialog's settings.
  const { sourceKey: currentKey } = picker
  useEffect(() => {
    if (carry.get()) return
    setOptions({})
    setPrintSequence(null)
    setSupports(null)
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
      // One action from the re-render to the save, as the Arrange dialog's (#2292).
      await traceAction(
        'arrange',
        // `scadbuddy.output_id` is the arranged output, as on the Arrange dialog's span
        // (#2891); the output it was arranged from is its own attribute.
        { 'scadbuddy.source_output_id': target.id, 'scadbuddy.rerendered': backfill ? 1 : 0 },
        async (within, span) => {
          let from = target
          if (backfill) {
            const { ready, failed } = await backfillOutputs([target], {
              signal: controller.signal,
              within,
              onProgress: (output, message) => setBackfillProgress({ [output.id]: message }),
            })
            const read = ready[0]
            if (!read) {
              setArrangeError(backfillFailures(failed, 'Re-arrange'))
              // Not a failure the span would see, nor a success (#2891): the same end
              // state, and value, as the Arrange dialog's when nothing was arranged.
              span.setAttribute('scadbuddy.outcome', 'nothing_arranged')
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
            { signal: controller.signal, within },
          )
          span.setAttribute('scadbuddy.output_id', next.output.id)
          // Closed while the output was being saved: it is saved, a normal output in
          // History, but this dialog keeps the output it was opened for.
          if (controller.signal.aborted) return
          carry.set({ plan, ready: false })
          setArranged(next.output)
          setArrangeNote(arrangedNote(next.plates))
        },
      )
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
  }, [open, scopeKey])

  const rememberedCopies =
    resolveOptions(
      remembered?.global_options,
      printerId === null ? undefined : remembered?.printers?.[String(printerId)],
      scopeKey === undefined ? undefined : remembered?.models?.[scopeKey],
    ).quantity ?? null
  const effectiveCopies = copies ?? rememberedCopies ?? 1

  /**
   * #284 — what the analyzers judge: this dialog's run request as `AnalysisRequest` takes
   * it (`backend/scadbuddy/analyzers/context.py:46`). It has no project. With
   * "All plates" the filament checks read every plate; the mesh checks read plate 1.
   * Not before the plan is seeded: an empty plan is judged once and then again (#2361).
   */
  const printChoices = printChoicesOf(selection)
  const allPlates = plate === 'all'
  const analysisRequest: AnalysisRequest | null =
    choices && printChoices && planReady
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
          // #2367: the sequence is a process override, so it changes what the run
          // slices; the check and the background slice must see it as the run does.
          // #1899, #717: supports and the profiles left on are process overrides too, and
          // Checks says when one can't apply.
          ...sliceOverrides(printSequence, supports, filamentProfiles.active),
        }
      : null
  const check = usePrintCheck(source, checkRequest)
  /**
   * #2169 — the choices on screen, sliced in the background once the check finds nothing
   * the run would refuse. A run of the same choices queues that slice.
   */
  const sliceable = check.current && check.verdict !== null && (check.verdict.errors ?? []).length === 0
  /**
   * #2169 — the plate the docked pane shows: the one chosen to print, or one clicked to
   * look at. "All plates" shows plate 1 until another is clicked.
   */
  const [clickedPlate, setClickedPlate] = useState<number | null>(null)
  const previewPlate = clickedPlate ?? (plate === 'all' ? 1 : plate)
  useEffect(() => {
    setClickedPlate(null)
  }, [plate, currentKey])
  const multiPlate = plates.length > 1
  const previewLabel = plateLabel(plates, previewPlate)
  // The plate the pane shows, sliced on its own: the run's own slice of it when it is
  // the plate chosen to print.
  const previewRequest =
    checkRequest && (allPlates || previewPlate !== plate)
      ? { ...checkRequest, plate_id: previewPlate, all_plates: false }
      : checkRequest
  const previewSlice = usePreviewSlice(source, sliceable && !sliced ? previewRequest : null)
  // #2703 — the slicer groups filaments itself, so once a slice of these choices for the
  // plate being printed has landed, its sides are the ones shown.
  // Memoised: `asSliced` builds a new plan on every call, and the effect below stores it,
  // so an unmemoised one re-rendered the dialog without end (#2737 review).
  const slicedShown = plate !== 'all' && previewPlate === plate && !previewSlice.stale && check.current
  const nozzlePlan = check.verdict?.nozzle_plan
  const sliceResult = previewSlice.result
  const slicedNow = useMemo(
    () => (slicedShown ? asSliced(nozzlePlan, sliceResult) : null),
    [slicedShown, nozzlePlan, sliceResult],
  )
  // #2737 review: previewing another plate does not unsay it. The printed plate's slice
  // still holds for these choices, so its sides stay until the choices or the plate change.
  const slicedFor = `${plate}|${currentKey}|${check.request}`
  const [lastSliced, setLastSliced] = useState<{ key: string; plan: NozzlePlanView } | null>(null)
  useEffect(() => {
    if (!slicedNow) return
    setLastSliced((last) =>
      last?.key === slicedFor && last.plan === slicedNow ? last : { key: slicedFor, plan: slicedNow },
    )
  }, [slicedNow, slicedFor])
  const slicedPlan =
    slicedNow ??
    (previewPlate !== plate && check.current && lastSliced?.key === slicedFor ? lastSliced.plan : null)
  /** #2164 — filled trays Bambuddy has no spool for, and the ones answered "no". */
  const trays = useDeclinedTrays(printerId, filaments?.trays ?? [])
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
  /**
   * #2365 — the background slice's grams, for the plate the slots describe: the one
   * chosen to print, when its result is of that plate. The result is kept, stale, while
   * the next slice runs or after it fails, so it can be another plate's (#2714 review):
   * the plate it was sliced for decides, not the plate on screen. "All plates" slices
   * plate 1 alone, which is not every plate's need.
   */
  const slicedForSlots = plate !== 'all' && previewSlice.plateId === plate ? previewSlice.result : null
  const pickerOptions = useMemo(
    () => (filaments ? withSlicedGrams(filaments, slicedForSlots) : null),
    [filaments, slicedForSlots],
  )
  // The check reads grams from the 3MF, which an unsliced upload does not carry, so a
  // spool short of what the slice needs is said here (#2365).
  // Not from a stale slice: Checks would state an old need as this one's.
  const slicedShort =
    slicedForSlots && !previewSlice.stale && pickerOptions
      ? checkPlan(pickerOptions, plan, effectiveCopies).filter(
          (warning) =>
            warning.kind === 'low-filament' &&
            !(check.verdict?.warnings ?? []).some(
              (known) => known.kind === 'low-filament' && known.slot_id === warning.slot_id,
            ),
        )
      : []
  const checkVerdict = check.verdict && {
    ...check.verdict,
    warnings: [...shown(check.verdict.warnings), ...slicedShort],
  }
  const verdict = (
    <>
      <RackNozzleLine rack={check.verdict?.rack} algorithm={rackAlgorithm} />
      <PrintVerdict verdict={checkVerdict} error={check.error} onRetry={check.reload} />
    </>
  )

  /**
   * #2672 — a batch step's seed, applied once: the printer first (its choices are read
   * again for it), then, once this file's spools are known, the rest.
   */
  const seeding = useRef<'printer' | 'rest' | 'done'>('printer')
  const { askPrinter, changeNozzles, changeQuality } = picker
  useEffect(() => {
    const seed = batch?.seed
    if (!seed || seeding.current === 'done' || !choices) return
    const wanted = seed.printer_id ?? null
    const known = wanted !== null && printers.some((entry) => entry.id === wanted)
    if (seeding.current === 'printer') {
      seeding.current = 'rest'
      if (known && wanted !== printerId) {
        askPrinter(wanted)
        return
      }
    }
    if ((known && printerId !== wanted) || !filaments || !planReady || (!project && ownProjects.loading)) return
    seeding.current = 'done'
    if (!project && 'project_id' in seed) setOwnProjectId(seed.project_id ?? null)
    const chosen = seed.filament_plan.slots ?? []
    const mine = slotKeys(filaments)
    const same = mine.length === batch.seedSlots.length && mine.every((key, at) => key === batch.seedSlots[at])
    if (!batch.seedIsOwn && !same) return
    const inventory = new Set((filaments.spools ?? []).map((spool) => spool.spool_id))
    setPlan((current) =>
      current.map((choice) => {
        const pick = chosen.find((entry) => entry.slot_id === choice.slot_id)
        return pick && inventory.has(pick.spool_id) ? { ...pick } : choice
      }),
    )
    const seedChoices = seed.choices
    if (!sliced && seedChoices) {
      if ((seedChoices.nozzles ?? []).length > 0) changeNozzles(seedChoices.nozzles ?? [])
      changeQuality({ tier: seedChoices.tier ?? null, processName: seedChoices.process_name ?? null })
    }
    setOptions(seed.options ?? {})
    if (seed.copies !== undefined && seed.copies !== null) setCopies(seed.copies)
    if (seed.print_sequence) setPrintSequence(seed.print_sequence)
    if (batch.seedIsOwn) setPlate(seed.all_plates ? 'all' : (seed.plate_id ?? 1))
  }, [
    batch,
    choices,
    printers,
    printerId,
    filaments,
    planReady,
    project,
    ownProjects.loading,
    sliced,
    askPrinter,
    changeNozzles,
    changeQuality,
    setPlan,
    setPlate,
  ])

  /** #2672 — Next: this step's run request, what the review lists, and what the check refuses. */
  function nextStep() {
    if (!batch || !printChoices) return
    const request = printRunBody({
      printerId,
      plan,
      printChoices,
      copies,
      plate,
      projectId,
      options,
      printSequence,
      rackPosition,
      rackAlgorithm: chosenAlgorithm,
    })
    const spools = filaments?.spools ?? []
    const estimate = sliced ? slicedEstimate(sliced, allPlates ? 1 : plate, filaments) : previewSlice.result
    const summary: BatchSummary = {
      printer: printer?.name ?? null,
      plate: allPlates ? 'All plates' : plateLabel(plates, plate),
      spools: plan.map((choice) => {
        const spool = spools.find((entry) => entry.spool_id === choice.spool_id)
        const name = spool ? [spool.material, spool.color_name ?? spool.colour].filter(Boolean).join(' ') : `spool ${choice.spool_id}`
        return `Slot ${choice.slot_id}: ${name}`
      }),
      print_seconds: estimate?.print_time_seconds ?? null,
      filament_g: estimate?.filament_used_g ?? null,
      slots: filaments ? slotKeys(filaments) : [],
      warnings: check.current ? shown(check.verdict?.warnings).map((warning) => warning.message) : [],
    }
    // A sliced file is never refused (#2671): what its check says is only ever a warning.
    const problems = check.current && !sliced ? (check.verdict?.errors ?? []) : []
    batch.onNext({ request, summary, problems })
  }

  function close() {
    // Escape and the backdrop are ignored mid-run, as Cancel is: a closed dialog would
    // reopen with Print enabled and send the print a second time (#539 review).
    // Also ignored mid-create (#710 review): unmounting the picker would drop the
    // creating guard while its request is still in flight.
    if (running || projectCreating) return
    // The page's project (#317) outlives the dialog; only its own copy is reset.
    setOwnProjectId(null)
    setOptions({})
    setPrintSequence(null)
    setSupports(null)
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
      title={batch ? batch.title : 'Print'}
      description={
        result || unanswered !== null
          ? undefined
          : sliced
            ? 'This file is sliced already. Choose the spools, and Bambuddy queues it as it is.'
            : picker.advanced
              ? 'Choose the spools, nozzles, quality and plate. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
              : 'Choose the spools. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
      }
      onClose={close}
      size={(choices || source) && !result && unanswered === null ? 'split' : 'md'}
      footer={
        batch ? (
          <>
            <Button onClick={close} disabled={batch.busy || projectCreating}>
              Cancel
            </Button>
            {batch.onBack && (
              <Button onClick={batch.onBack} disabled={batch.busy}>
                Back
              </Button>
            )}
            <Button onClick={batch.onSkip} disabled={batch.busy} data-testid="batch-skip">
              Skip
            </Button>
            <Button
              variant="primary"
              onClick={nextStep}
              disabled={batch.busy || loading || !choices || !planReady || ownProjects.loading || projectCreating}
              data-testid="batch-next"
            >
              {batch.busy && <Spinner />}
              {batch.last ? 'Review' : 'Next'}
            </Button>
          </>
        ) : result ? (
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
                running ||
                loading ||
                !choices ||
                !planReady ||
                refused ||
                runRefuses ||
                ownProjects.loading ||
                projectCreating
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
          plates={plates}
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
          {batch?.strip}
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

          {/* #2550 — the plates and the preview need only the source, so a choices read
              that is slow or failed (its error and Retry above) never hides them. Only
              the spool, printer and print controls wait for the choices. */}
          {(choices || source) && (
            <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(300px,380px)]">
            <div className="min-w-0 space-y-3">
              {choices && (
                <>
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
                    <div className="flex items-center gap-3" data-testid="filaments-unavailable">
                      <p className="text-[12px] text-warn">
                        ScadBuddy could not read the filament inventory: {filamentError} Print is unavailable
                        until it loads.
                      </p>
                      <Button size="sm" onClick={retryFilaments}>
                        Retry
                      </Button>
                    </div>
                  )}
                  {sliced && <SlicedSettings profile={sliced} />}
                  {!picker.advanced && !sliced && (
                    <FlowChoice value={nozzles[0]?.flow ?? 'standard'} onChange={picker.changeFlow} />
                  )}
                  {printerId !== null &&
                    trays.open.map((tray) => (
                      <TrayQuestion
                        key={`${tray.ams_id}:${tray.tray_id}`}
                        printerId={printerId}
                        tray={tray}
                        spools={filaments?.spools ?? []}
                        onAssigned={picker.reload}
                        onDecline={() => trays.decline(tray)}
                      />
                    ))}
                  {filaments && (
                    <FilamentPicker
                      options={pickerOptions ?? filaments}
                      plan={plan}
                      onChange={setPlan}
                      copies={effectiveCopies}
                      offeredTrays={trays.offered}
                      gramsStale={slicedForSlots !== null && previewSlice.stale}
                    />
                  )}
                  {!sliced && (
                    <NozzlePlanLine
                      plan={slicedPlan ?? check.verdict?.nozzle_plan}
                      stale={!check.current}
                      sliced={slicedPlan !== null}
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

                  {/* #717: outside Advanced, since a suggested profile is on until switched off. */}
                  {!sliced && (
                    <FilamentProfilesStep suggestions={filamentProfiles.suggestions} onToggle={filamentProfiles.toggle} />
                  )}

                  {picker.advanced && !sliced && filaments && (filaments.slots ?? []).length > 0 && (
                    <PresetOverrides
                      size={size}
                      slots={filaments.slots ?? []}
                      presets={choices.filament_presets?.[size] ?? []}
                      overrides={overrides}
                      onChange={picker.setOverride}
                    />
                  )}
                </>
              )}

              {/* #986 — one plate has no choice to make, but its name still says what prints. */}
              {plates.length === 1 && plates[0]?.name && (
                <p className="text-[13px]" data-testid="single-plate">
                  <span className="text-muted">Plate:</span>{' '}
                  <button
                    type="button"
                    onClick={() => setClickedPlate(1)}
                    className="underline decoration-dotted underline-offset-2 hover:text-accent"
                    title="Show it in the preview"
                  >
                    {plates[0].name}
                  </button>
                </p>
              )}

              {plates.length > 1 && source && (
                <PlatesToPrint
                  plates={plates}
                  value={plate}
                  onChange={picker.setPlate}
                  thumbnailUrl={sourceApi(source).plateThumbnailUrl}
                  previewed={previewPlate}
                  onPreview={setClickedPlate}
                />
              )}

              {source && !sliced && (
                <div>
                  <Button size="sm" variant="ghost" onClick={() => setPreviewOpen(true)} data-testid="plate-preview-open">
                    Preview in 3D
                  </Button>
                  {/* Mounted only while open, keyed by plate: each open starts on Colors,
                      with no cut left over from another plate. */}
                  {previewOpen && (
                    <PlatePreviewDialog
                      key={plate}
                      open
                      onClose={() => setPreviewOpen(false)}
                      url={sourceApi(source).previewUrl(plate === 'all' ? 1 : plate)}
                      label={
                        // An output's preview is the whole output; a library file's, one
                        // plate, the first when every plate prints.
                        source.kind === 'output' && plates.length > 1
                          ? 'Every plate'
                          : plate === 'all' && plates.length > 1
                            ? `${plateLabel(plates, 1)} (first of ${plates.length} plates)`
                            : plateLabel(plates, plate === 'all' ? 1 : plate)
                      }
                      colors={previewColors}
                      toolpathUrl={
                        // #2072 — the background slice's toolpaths, while they are of the
                        // choices on screen and of the plate this dialog shows. A multi-plate
                        // output's dialog shows every plate, which no one plate's slice is.
                        previewSlice.result &&
                        !previewSlice.stale &&
                        !(source.kind === 'output' && picker.plates.length > 1) &&
                        previewPlate === (plate === 'all' ? 1 : plate)
                          ? sourceApi(source).toolpathUrl(previewSlice.result.job_id)
                          : null
                      }
                    />
                  )}
                </div>
              )}

              {choices && (
                <>
                  {/* #1660 — Simple hides the project step, but not where the print is filed. */}
                  {!picker.advanced && filedUnderLabel !== null && (
                    <p className="text-[13px] text-muted" data-testid="print-filed-under">
                      Filed under: <span className="text-ink">{filedUnderLabel}</span>{' '}
                      <button
                        type="button"
                        onClick={() => {
                          focusProject.current = true
                          picker.toggleAdvanced()
                        }}
                        disabled={projectCreating}
                        className="underline decoration-dotted underline-offset-2 hover:text-ink"
                        data-testid="print-filed-under-change"
                      >
                        (change)
                      </button>
                    </p>
                  )}

                  {/* #768 — Simple mode sends these steps' defaults without showing them. */}
                  {picker.advanced && (
                    <>
                      {!sliced && (
                        <>
                          <NozzleStep
                            sizes={choices.nozzle_sizes ?? []}
                            installed={choices.installed ?? []}
                            value={nozzles}
                            onChange={picker.changeNozzles}
                          />
                          <NozzlePlanStep plan={check.verdict?.nozzle_plan} sides={selection.sides} onSide={picker.setSide} />
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
                              labels={choices.bed_type_labels ?? {}}
                              value={bedType}
                              lastBedType={choices.last_bed_type ?? null}
                              printerName={printer?.name ?? null}
                              onChange={picker.setBedType}
                            />
                          )}

                          <PrintSequenceStep
                            value={printSequence}
                            onChange={setPrintSequence}
                            objects={check.current ? (check.verdict?.objects ?? null) : null}
                          />

                          <SupportsStep
                            value={supports}
                            onChange={setSupports}
                            templateSettings={given?.kind === 'output' ? printSettings : undefined}
                            fromFile={given?.kind !== 'output'}
                          />

                          {given?.kind === 'output' && (
                            <SlicerDefaults
                              settings={printSettings}
                              overridden={[
                                ...(printSequence !== null ? ['print_sequence'] : []),
                                ...overriddenSupportKeys(supports),
                              ]}
                            />
                          )}
                        </>
                      )}

                      <PrintOptionsDisclosure
                        subject={subject}
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

                  {/* The analyzers judge what the print slices: an output's 3MF, or a library
                      file's, read as a print of it reads it (#1753). */}
                  {/* #2671 — a sliced file's mesh is not read, so only the verdict shows. */}
                  {sliced ? (
                    verdict
                  ) : (
                    <AnalyzerPanel target={analysisTarget} request={analysisRequest} allPlates={allPlates}>
                      {verdict}
                    </AnalyzerPanel>
                  )}
                </>
              )}
            </div>
            <div className="min-w-0 lg:sticky lg:top-0 lg:self-start">
              <PreviewPane
                key={previewPlate}
                // A sliced file has no mesh ScadBuddy reads: its plate image stands in.
                url={source && !sliced ? sourceApi(source).previewUrl(previewPlate, multiPlate) : null}
                thumbnailUrl={source ? sourceApi(source).plateThumbnailUrl(previewPlate) : null}
                label={multiPlate || plates[0]?.name ? previewLabel : null}
                slots={filaments?.slots ?? []}
                spools={filaments?.spools ?? []}
                plan={plan}
                colors={previewColors}
                slice={sliced ? slicedEstimate(sliced, previewPlate, filaments, plate) : previewSlice.result}
                nozzlePlan={previewSlice.plan}
                plateSlots={slicedPlateSlots(filaments?.slots ?? [], {
                  // The plates offered: a sliced file's own, not every plate Bambuddy lists.
                  plates: plates.length,
                  chosen: plate,
                  sliced: previewSlice.plateId,
                })}
                stale={previewSlice.stale}
                slicing={previewSlice.slicing}
                sliceError={previewSlice.error}
              />
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
