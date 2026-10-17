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
  PrimeTowerChoice,
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
import { firstPlate, offeredChoice, plateFields, plateKey, platesSummary, singlePlate, withPlate } from '../lib/plateChoice'
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
import { PrimeTowerStep } from './print/PrimeTowerStep'
import { overriddenTowerKeys } from '../lib/primeTower'
import { overriddenSupportKeys } from '../lib/supports'
import { SlicerDefaults } from './print/SlicerDefaults'
import { SlicedSettings } from './print/SlicedSettings'
import { slicedEstimate } from '../lib/slicedEstimate'
import { QualityStep } from './print/QualityStep'
import { QueuedPanel } from './print/QueuedPanel'
import { PlateClearNotice } from './print/PlateClearNotice'
import { RackNozzleLine, RackNozzleStep } from './print/RackNozzle'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'
import { breadcrumbs, type ProjectList, useProjectList } from '../lib/projects'
import { ProjectPicker } from './ProjectPicker'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Spinner } from './ui/spinner'
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
/** What a batch step saves: its run request, what the review lists, what the check refuses. */
export interface BatchStepChoice {
  request: PrintRunRequest
  summary: BatchSummary
  problems: string[]
}

export interface BatchStep {
  /** "File 2 of 5 — name". */
  title: string
  /** The files strip, given `jump`: a strip button leaves through it, so the step's
   * choices are saved as Next saves them (#2718 review). */
  strip: (jump: (step: number) => void) => ReactNode
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
  onNext: (choice: BatchStepChoice) => void
  /** A strip button: to `step`, with this step's choices when it has any to save. */
  onJump: (step: number, choice: BatchStepChoice | null) => void
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
  /**
   * #2288 — History's Queue: the same dialog with "Wait for a manual start" ticked, so
   * the print waits in Bambuddy's queue rather than starting. It can be unticked.
   */
  queue?: boolean
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
  queue = false,
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
  const { nozzles, tier, processName, bedType, overrides } = selection
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
  /**
   * #3652 — the plates that print, over the plates offered: every plate unless some are
   * chosen, one plate (or a list not read yet) being that plate, and a sliced file's
   * only those it holds G-code for.
   */
  const plate = useMemo(
    () =>
      offeredChoice(
        selection.plate,
        plates.length > 0 ? plates.map((entry) => entry.index) : slicedPlates,
      ),
    [selection.plate, plates, slicedPlates],
  )
  const single = singlePlate(plate)
  const first = firstPlate(plate)
  /** Nothing prints while every plate is unticked (#3652). */
  const noPlate = plate !== 'all' && plate.length === 0
  const { filaments, plan, setPlan, planChanged, filamentError, planReady, retryFilaments } = useFilamentPlan(
    source,
    choices,
    plate,
    carry,
  )

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
  /** The options this print sends: a Queue's start waits for a hand, unless unticked (#2288). */
  // Memoised: it is a dependency of the run and of the background analysis.
  // Seeded only while it was never chosen. `null` is the user handing it back to the
  // remembered value or Bambuddy's ("the default"): sent as no override at all.
  const sentOptions = useMemo(() => {
    if (!queue) return options
    const { manual_start: start, ...rest } = options
    if (start === undefined) return { ...rest, manual_start: true }
    return start === null ? rest : options
  }, [queue, options])
  /**
   * #2786 review — the disclosure's edits, kept as the user's own: it builds each edit
   * from the value it shows, which in a Queue carries the seed. The seed passing back
   * through is not a choice; Queue's start dropped from what it showed is "the default".
   */
  const editOptions = (next: PrintOptions) => {
    if (!queue) return setOptions(next)
    const { manual_start: start, ...rest } = next
    if (start === undefined) return setOptions({ ...rest, manual_start: null })
    // A `true` while never chosen is the seed passing back through, not a choice. The
    // select already shows Yes then, so picking Yes fires nothing either: to remember
    // Yes, the user picks No and then Yes, and that one is kept.
    setOptions(start === true && options.manual_start === undefined ? rest : next)
  }
  /** What Remember saves: the user's edits, never Queue's seed or an unset. */
  const rememberOptions = useMemo(() => {
    const { manual_start: start, ...rest } = options
    return start === null || start === undefined ? rest : options
  }, [options])
  /**
   * #1862 — this print's sequence, `null` for the template's or the process's. Per print,
   * never remembered (until #1754 decides remembered choices by subject).
   */
  const [printSequence, setPrintSequence] = useState<PrintSequence | null>(null)
  // #1899 — this print's supports; `null` leaves them to the template and the process.
  const [supports, setSupports] = useState<SupportsChoice | null>(null)
  // #3628 — this print's prime tower; `null` leaves it to the template and the process.
  const [primeTower, setPrimeTower] = useState<PrimeTowerChoice | null>(null)
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
  // The same for the prime tower (#3648 review): Simple mode sends none.
  useEffect(() => {
    if (!advanced) {
      setRackPosition(null)
      setPrimeTower(null)
    }
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
  /**
   * #3771 — "(change)" shows the project picker where the line was, in Simple mode
   * still: only the Advanced switch changes the mode. The focus goes to the picker.
   */
  const [changingProject, setChangingProject] = useState(false)
  // Advanced shows the picker in its own step; back in Simple, the line names the choice.
  useEffect(() => {
    if (!open || picker.advanced) setChangingProject(false)
  }, [open, picker.advanced])
  const focusProject = useRef(false)
  useEffect(() => {
    if (!changingProject || !focusProject.current) return
    focusProject.current = false
    const select = document.getElementById('print-project')
    select?.scrollIntoView?.({ block: 'nearest' })
    select?.focus()
  }, [changingProject])

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
  /** #79 — a send to a project uploads into that project's folder. */
  const projectPicker = project ? (
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
  )

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
    selection: { ...selection, plate },
    plan,
    planChanged,
    copies,
    projectId,
    options: sentOptions,
    printSequence,
    supports,
    primeTower,
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
  const { reset: resetProfiles } = filamentProfiles
  useEffect(() => {
    if (carry.get()) return
    setOptions({})
    setPrintSequence(null)
    setSupports(null)
    setPrimeTower(null)
    resetProfiles()
  }, [currentKey, carry, resetProfiles])

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
                // #3637 — each colouring of a part is its own entry: named by its colours.
                recolour: object.recolour ?? {},
              })),
              goal: arrangeGoal,
              printer_id: printerId,
              filament_plan: { slots: plan, force_colour_match: false },
              // #3637 — every colour a recoloured copy prints in has its slot, whether
              // or not this output's `colors` already lists it.
              colours: [
                ...new Set([
                  // Upper case on both sides, as the API compares them: one colour, one slot.
                  ...(from.colors ?? []).map((c) => c.toUpperCase()),
                  ...(from.manifest ?? []).flatMap((object) =>
                    Object.values(object.recolour ?? {}).map((c) => c.toUpperCase()),
                  ),
                ]),
              ],
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
  const analysisRequest: AnalysisRequest | null =
    choices && printChoices && planReady
      ? {
          printer_id: printerId,
          filament_plan: { slots: plan, force_colour_match: false },
          choices: printChoices,
          ...plateFields(plate),
          copies: effectiveCopies,
          options: sentOptions,
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
          ...plateFields(plate),
          rack_position: rackPosition,
          rack_algorithm: chosenAlgorithm,
          // #2367: the sequence is a process override, so it changes what the run
          // slices; the check and the background slice must see it as the run does.
          // #1899, #717: supports and the profiles left on are process overrides too, and
          // Checks says when one can't apply.
          // #3628: and so is the prime tower.
          ...sliceOverrides(printSequence, supports, filamentProfiles.active, primeTower),
        }
      : null
  const check = usePrintCheck(source, checkRequest)
  /**
   * #2169 — the choices on screen, sliced in the background once the check finds nothing
   * the run would refuse. A run of the same choices queues that slice.
   */
  const sliceable = check.current && check.verdict !== null && (check.verdict.errors ?? []).length === 0
  /**
   * #2169, #3652 — the plate the docked pane shows: the last one ticked or unticked, else
   * the first that prints.
   */
  const [clickedPlate, setClickedPlate] = useState<number | null>(null)
  const previewPlate = clickedPlate ?? first
  useEffect(() => {
    setClickedPlate(null)
  }, [currentKey])
  const multiPlate = plates.length > 1
  const previewLabel = plateLabel(plates, previewPlate)
  // The plate the pane shows, sliced on its own: the run's own slice of it when it is
  // the plate chosen to print.
  const previewRequest =
    checkRequest && previewPlate !== single ? withPlate(checkRequest, previewPlate) : checkRequest
  const previewSlice = usePreviewSlice(source, sliceable && !sliced ? previewRequest : null)
  // #2703 — the slicer groups filaments itself, so once a slice of these choices for the
  // plate being printed has landed, its sides are the ones shown.
  // Memoised: `asSliced` builds a new plan on every call, and the effect below stores it,
  // so an unmemoised one re-rendered the dialog without end (#2737 review).
  const slicedShown = single !== null && previewPlate === single && !previewSlice.stale && check.current
  const nozzlePlan = check.verdict?.nozzle_plan
  const sliceResult = previewSlice.result
  const slicedNow = useMemo(
    () => (slicedShown ? asSliced(nozzlePlan, sliceResult) : null),
    [slicedShown, nozzlePlan, sliceResult],
  )
  // #2737 review: previewing another plate does not unsay it. The printed plate's slice
  // still holds for these choices, so its sides stay until the choices or the plate change.
  const slicedFor = `${plateKey(plate)}|${currentKey}|${check.request}`
  const [lastSliced, setLastSliced] = useState<{ key: string; plan: NozzlePlanView } | null>(null)
  useEffect(() => {
    // New choices or other plates: the last slice's sides hold only for its own key, so
    // they are shown for nothing else (#2737 review). They are kept, not dropped, so a
    // plate ticked and unticked again comes back to them (#3652): the same key is the
    // same choices on the same plate.
    if (!slicedNow) return
    setLastSliced((last) =>
      last?.key === slicedFor && last.plan === slicedNow ? last : { key: slicedFor, plan: slicedNow },
    )
  }, [slicedNow, slicedFor])
  const slicedPlan =
    slicedNow ??
    (previewPlate !== single && check.current && lastSliced?.key === slicedFor ? lastSliced.plan : null)
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
   * the plate it was sliced for decides, not the plate on screen. Several plates' slots
   * are not one sliced plate's need.
   */
  const slicedForSlots = single !== null && previewSlice.plateId === single ? previewSlice.result : null
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
  const { askPrinter, changeNozzles, changeQuality, setPlate } = picker
  const restoreProfiles = filamentProfiles.restore
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
    if (batch.seedIsOwn) {
      // Its own choices, the ones a mount resets to the defaults (#2718 review): saving
      // the step again without them would queue it without the supports, tower or
      // profiles the user chose.
      setPlate(seed.all_plates ? 'all' : seed.plate_ids?.length ? seed.plate_ids : [seed.plate_id ?? 1])
      setSupports(seed.supports ?? null)
      setPrimeTower(seed.prime_tower ?? null)
      restoreProfiles(seed.filament_profiles ?? [])
    }
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
    restoreProfiles,
  ])

  /** #2672 — this step's run request, what the review lists, and what the check refuses. */
  function stepChoice(): BatchStepChoice | null {
    if (!batch || !printChoices) return null
    const request = printRunBody({
      printerId,
      plan,
      printChoices,
      copies,
      plate,
      projectId,
      options: sentOptions,
      printSequence,
      // What Print would send (#1899, #3628, #717): a batch step keeps them for Queue all.
      supports,
      primeTower,
      filamentProfiles: filamentProfiles.active,
      rackPosition,
      rackAlgorithm: chosenAlgorithm,
    })
    const spools = filaments?.spools ?? []
    const estimate = sliced ? slicedEstimate(sliced, first, filaments, single ?? 'all') : previewSlice.result
    const summary: BatchSummary = {
      printer: printer?.name ?? null,
      plate: single === null ? platesSummary(plate, plates) : plateLabel(plates, single),
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
    return { request, summary, problems }
  }

  function nextStep() {
    const choice = stepChoice()
    if (batch && choice) batch.onNext(choice)
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
    setPrimeTower(null)
    filamentProfiles.reset()
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

  const description =
    result || unanswered !== null
      ? undefined
      : sliced
        ? 'This file is sliced already. Choose the spools, and Bambuddy queues it as it is.'
        : picker.advanced
          ? 'Choose the spools, nozzles, quality and plate. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
          : 'Choose the spools. ScadBuddy picks the Bambu presets, then Bambuddy slices and queues it.'
  const split = (choices || source) && !result && unanswered === null
  const footer = batch ? (
          <>
            <Button type="button" variant="outline" onClick={close} disabled={batch.busy || projectCreating}>
              Cancel
            </Button>
            {batch.onBack && (
              <Button type="button" variant="outline" onClick={batch.onBack} disabled={batch.busy}>
                Back
              </Button>
            )}
            <Button type="button" variant="outline" onClick={batch.onSkip} disabled={batch.busy} data-testid="batch-skip">
              Skip
            </Button>
            <Button
              type="button"
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
            <Button type="button" variant="outline" onClick={close}>
              Done
            </Button>
            {result.bambuddy_url && (
              <Button type="button" onClick={() => openExternal(bambuddyLink(result.bambuddy_url as string))}>
                Open in queue
              </Button>
            )}
          </>
        ) : unanswered !== null ? (
          <>
            <Button type="button" variant="outline" onClick={close}>
              Close
            </Button>
            {bambuddyUrl ? (
              <Button type="button" onClick={() => openExternal(`${bambuddyUrl}/queue`)}>
                {"Open Bambuddy's queue"}
              </Button>
            ) : (
              settings.error && (
                <Button type="button" variant="outline" onClick={settings.reload}>
                  {"Find Bambuddy's queue"}
                </Button>
              )
            )}
          </>
        ) : (
          <>
            <Button type="button" variant="outline" onClick={close} disabled={running || projectCreating}>
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => void run()}
              // Held while the dialog's own project list loads, so a Simple-mode print
              // cannot go out before the last project has seeded it.
              disabled={
                running ||
                loading ||
                !choices ||
                noPlate ||
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
              {queue ? 'Queue' : 'Print'}
            </Button>
          </>
        )

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close()
      }}
    >
      {/* Bounded by the visible height, its body scrolling, so the title and Print stay
          in reach however tall the choices grow (#87, #1723). */}
      <DialogContent
        showCloseButton={false}
        className={cn('flex max-h-[calc(100dvh-2rem)] flex-col gap-0 p-0', split ? 'sm:max-w-[1120px]' : 'sm:max-w-lg')}
      >
        <DialogHeader className="shrink-0 border-b px-5 py-4">
          <DialogTitle>{batch ? batch.title : queue ? 'Queue' : 'Print'}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
      {result ? (
        <QueuedPanel
          result={{ ...result, warnings: shown(result.warnings) }}
          printerName={printer?.name ?? null}
          progress={progress}
          polling={polling}
          plates={plates}
        />
      ) : unanswered !== null ? (
        <p role="alert" className="text-sm text-destructive" data-testid="run-unanswered">
          {unanswered}{' '}
          {
            "The print may still have been queued. Check Bambuddy's queue before printing again, or it may print twice."
          }
        </p>
      ) : (
        <>
          {batch?.strip((step) => batch.onJump(step, stepChoice()))}
          {loading && !choices && (
            <div className="mb-4 space-y-2" data-testid="print-loading">
              <p className="text-sm text-muted-foreground">Reading the printer and the inventory</p>
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          )}

          {loadError && (
            <div className="mb-4 flex flex-wrap items-baseline gap-x-3">
              <p role="alert" className="text-sm text-destructive">
                {loadError}
              </p>
              {/* #482: a Bambuddy blip or timeout should not need the dialog reopened. */}
              <Button type="button" variant="link" size="sm" className="h-auto px-0" onClick={picker.reload}>
                Retry
              </Button>
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
                    <div className="grid gap-1.5">
                      <Label htmlFor="print-printer">Printer</Label>
                      <Select
                        value={printerId === null ? '' : String(printerId)}
                        onValueChange={(next) => picker.askPrinter(Number(next))}
                      >
                        <SelectTrigger id="print-printer" className="w-full">
                          <SelectValue placeholder="Choose a printer" />
                        </SelectTrigger>
                        <SelectContent>
                          {printers.map((entry) => (
                            <SelectItem key={entry.id} value={String(entry.id)}>
                              {entry.name}
                              {entry.model ? ` (${entry.model})` : ''}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  )}
                  {printerId !== null && <PlateClearNotice printerId={printerId} />}

                  {filamentError && (
                    <div className="flex items-center gap-3" data-testid="filaments-unavailable">
                      <p className="text-xs text-destructive">
                        ScadBuddy could not read the filament inventory: {filamentError} Print is unavailable
                        until it loads.
                      </p>
                      <Button type="button" size="sm" variant="outline" onClick={retryFilaments}>
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
                    <Card size="sm" role="group" aria-labelledby="print-arrange">
                      <CardHeader>
                        <CardTitle id="print-arrange" className="text-sm">
                          Arrange
                        </CardTitle>
                      </CardHeader>
                      <CardContent className="space-y-2">
                      {stale && <p className="text-xs text-muted-foreground">{backfillNote([target])}</p>}
                      <div className="flex flex-wrap items-end gap-2">
                      <div className="grid min-w-0 flex-1 gap-1.5">
                      <Label htmlFor="arrange-for" className="text-xs text-muted-foreground">
                        Arrange for
                      </Label>
                      <Select value={arrangeGoal} onValueChange={(next) => setArrangeGoal(next as ArrangeGoal)}>
                        <SelectTrigger id="arrange-for" className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {Object.entries(GOAL_LABELS)
                            .filter(([value]) => value !== 'keep_together')
                            .map(([value, text]) => (
                              <SelectItem key={value} value={value}>
                                {text}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        disabled={arranging || askBackfill}
                        onClick={() => (stale ? setAskFor(target.id) : void rearrange(false))}
                      >
                        Re-arrange for these spools
                      </Button>
                      </div>
                      {askBackfill && (
                        <div>
                          <BackfillPrompt
                            outputs={[target]}
                            onConfirm={() => void rearrange(true)}
                            onCancel={() => setAskFor(null)}
                          />
                        </div>
                      )}
                      <BackfillProgress outputs={[target]} progress={backfillProgress} />
                      {arrangeNote && (
                        <p aria-live="polite" className="text-xs text-muted-foreground">
                          {arrangeNote}
                        </p>
                      )}
                      {arrangeError && (
                        <p role="alert" className="text-sm text-destructive">
                          {arrangeError}
                        </p>
                      )}
                      </CardContent>
                    </Card>
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
                <p className="text-sm" data-testid="single-plate">
                  <span className="text-muted-foreground">Plate:</span>{' '}
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button type="button" variant="link" className="h-auto p-0" onClick={() => setClickedPlate(1)}>
                        {plates[0].name}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Show it in the preview</TooltipContent>
                  </Tooltip>
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
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setPreviewOpen(true)}
                    data-testid="plate-preview-open"
                  >
                    Preview in 3D
                  </Button>
                  {/* Mounted only while open, keyed by plate: each open starts on Colors,
                      with no cut left over from another plate. */}
                  {previewOpen && (
                    <PlatePreviewDialog
                      key={plateKey(plate)}
                      open
                      onClose={() => setPreviewOpen(false)}
                      url={sourceApi(source).previewUrl(first)}
                      label={
                        // An output's preview is the whole output; a library file's, one
                        // plate, the first of several that print.
                        source.kind === 'output' && plates.length > 1
                          ? 'Every plate'
                          : single === null && plates.length > 1
                            ? `${plateLabel(plates, first)} (first of ${plate === 'all' ? plates.length : plate.length} plates)`
                            : plateLabel(plates, first)
                      }
                      colors={previewColors}
                      toolpathUrl={
                        // #2072 — the background slice's toolpaths, while they are of the
                        // choices on screen and of the plate this dialog shows. A multi-plate
                        // output's dialog shows every plate, which no one plate's slice is.
                        previewSlice.result &&
                        !previewSlice.stale &&
                        !(source.kind === 'output' && picker.plates.length > 1) &&
                        previewPlate === first
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
                  {!picker.advanced && changingProject && projectPicker}
                  {!picker.advanced && !changingProject && filedUnderLabel !== null && (
                    <p className="text-sm text-muted-foreground" data-testid="print-filed-under">
                      Filed under: <span className="text-foreground">{filedUnderLabel}</span>{' '}
                      <Button
                        type="button"
                        variant="link"
                        size="sm"
                        className="h-auto px-0"
                        onClick={() => {
                          focusProject.current = true
                          setChangingProject(true)
                        }}
                        disabled={projectCreating}
                        data-testid="print-filed-under-change"
                      >
                        (change)
                      </Button>
                    </p>
                  )}

                  {/* #3635 — Simple mode shows no Options, but never hides a remembered
                      one that holds the job. */}
                  {!picker.advanced && (
                    <PrintOptionsDisclosure
                      heldOnly
                      known={remembered}
                      subject={subject}
                      printerId={printerId}
                      value={sentOptions}
                      toRemember={rememberOptions}
                      onChange={editOptions}
                    />
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

                          <PrimeTowerStep
                            value={primeTower}
                            onChange={setPrimeTower}
                            templateSettings={given?.kind === 'output' ? printSettings : undefined}
                            fromFile={given?.kind !== 'output'}
                            // The filament step is the chosen plate's (useFilamentPlan reads it per
                            // plate), so these are that plate's filaments; null while they load.
                            colours={filaments ? (filaments.slots ?? []).length : null}
                          />

                          {given?.kind === 'output' && (
                            <SlicerDefaults
                              settings={printSettings}
                              overridden={[
                                ...(printSequence !== null ? ['print_sequence'] : []),
                                ...overriddenSupportKeys(supports),
                                ...overriddenTowerKeys(primeTower),
                              ]}
                            />
                          )}
                        </>
                      )}

                      <PrintOptionsDisclosure
                        subject={subject}
                        printerId={printerId}
                        value={copies === null ? sentOptions : { ...sentOptions, quantity: copies }}
                        toRemember={copies === null ? rememberOptions : { ...rememberOptions, quantity: copies }}
                        onChange={({ quantity, ...rest }) => {
                          setCopies(quantity ?? null)
                          editOptions(rest)
                        }}
                      />

                      {projectPicker}

                      <CopiesField value={copies} remembered={rememberedCopies} onChange={setCopies} />
                    </>
                  )}

                  {/* The analyzers judge what the print slices: an output's 3MF, or a library
                      file's, read as a print of it reads it (#1753). */}
                  {/* #2671 — a sliced file's mesh is not read, so only the verdict shows. */}
                  {sliced ? (
                    verdict
                  ) : (
                    <AnalyzerPanel target={analysisTarget} request={analysisRequest} allPlates={single === null}>
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
                slice={sliced ? slicedEstimate(sliced, previewPlate, filaments, single ?? 'all') : previewSlice.result}
                nozzlePlan={previewSlice.plan}
                plateSlots={slicedPlateSlots(filaments?.slots ?? [], {
                  // The plates offered: a sliced file's own, not every plate Bambuddy lists.
                  plates: plates.length,
                  chosen: single ?? 'all',
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
            <p role="alert" className="mt-3 text-sm text-destructive">
              {runError}
            </p>
          )}
        </>
      )}
        </div>
        <DialogFooter className="m-0 shrink-0 flex-row flex-wrap items-center justify-end px-5 py-3">{footer}</DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
