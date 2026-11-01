import type { Attributes } from '@opentelemetry/api'
import { useEffect, useRef, useState } from 'react'
import { api, ApiError, mayHaveRun, newRequestId } from '../api/client'
import type {
  ChoicesView,
  PrintOptions,
  PrintRunRequest,
  RegistryRunRequest,
  PrintRunResult,
  PrintSequence,
  PrimeTowerChoice,
  SupportsChoice,
  RackAlgorithm,
  SlotChoice,
} from '../api/types'
import { type PlateChoice, plateFields, plateKey } from './plateChoice'
import { printChoicesOf, sliceOverrides } from './printChoices'
import { sourceApi, type PrintSource } from './printSource'
import { traceAction } from './traceAction'
import type { PrintSelection } from './usePrintChoices'

interface RunInput {
  /** #313 — an output or a library file. */
  source: PrintSource | undefined
  choices: ChoicesView | null
  printerId: number | null
  /** #3823 — the registry printer that links `printerId`, when the registry has one. */
  registryPrinter?: string | null
  selection: PrintSelection
  plan: SlotChoice[]
  /** Whether `plan` differs from the suggestion, so it is worth remembering (#78). */
  planChanged: boolean
  /** `null` leaves the quantity to the remembered options (#124). */
  copies: number | null
  /**
   * #79 — the Bambuddy project this print is filed under; `null` is "No project".
   * `undefined` while the project list is unknown leaves the field out, so the server
   * applies the default project (`last_project_id`) rather than an explicit "No project" (#1045).
   */
  projectId: number | null | undefined
  /** #3651 — a library folder of no project chosen instead of a project; `projectId` is then left out. */
  folderId?: number | null
  /** #88 — this print's overrides, all but `quantity`, which is `copies`. */
  options: PrintOptions
  /** #1862 — this print's sequence; `null` leaves it to the template and the process. */
  printSequence: PrintSequence | null
  /** #1899 — this print's supports; `null` (or absent) leaves them to the template and the process. */
  supports?: SupportsChoice | null
  /** #3628 — this print's prime tower; `null` (or absent) leaves it to the template and the process. */
  primeTower?: PrimeTowerChoice | null
  /** #717 — the filament profiles left on, by id. */
  filamentProfiles?: string[]
  /** #836 — a hand-picked rack position, or `null` for Automatic. */
  rackPosition: number | null
  /** #836 — the rack algorithm chosen in this dialog; `null` uses the printer's remembered one. */
  rackAlgorithm: RackAlgorithm | null
  onRan: (result: PrintRunResult) => void
}

/**
 * Spec 2026-10-01 §6: what is printed, on which printer, and which plate. Every print
 * says which kind of source it was (#1231), ScadBuddy's own word; a library file's id is
 * Bambuddy's, which §6 never records, so only an output names its source.
 */
function printAttributes(source: PrintSource, body: PrintRunRequest): Attributes {
  return {
    'scadbuddy.print_source': source.kind,
    ...(source.kind === 'output' ? { 'scadbuddy.output_id': source.output.id } : {}),
    ...(typeof body.printer_id === 'number' ? { 'scadbuddy.printer_id': body.printer_id } : {}),
    'scadbuddy.plate_id': body.plate_id,
    'scadbuddy.all_plates': body.all_plates,
  }
}

const NONE: string[] = []

/**
 * The run request the dialog's choices make, without a press's `request_id`: what Print
 * sends, and what a batch print's step keeps for Queue all to send (#2672).
 */
export function printRunBody({
  printerId,
  registryPrinter = null,
  plan,
  printChoices,
  copies,
  plate,
  projectId,
  folderId = null,
  options,
  printSequence,
  supports = null,
  primeTower = null,
  filamentProfiles = [],
  rackPosition,
  rackAlgorithm,
}: {
  printerId: number | null
  /** #3823 — sent as `printer`, beside `printer_id`, so a backend of either age reads it. */
  registryPrinter?: string | null
  plan: SlotChoice[]
  printChoices: NonNullable<ReturnType<typeof printChoicesOf>>
  copies: number | null
  plate: PlateChoice
  projectId: number | null | undefined
  folderId?: number | null
  options: PrintOptions
  printSequence: PrintSequence | null
  supports?: SupportsChoice | null
  primeTower?: PrimeTowerChoice | null
  filamentProfiles?: readonly string[]
  rackPosition: number | null
  rackAlgorithm: RackAlgorithm | null
}): RegistryRunRequest {
  return {
    ...(registryPrinter ? { printer: registryPrinter } : {}),
    printer_id: printerId,
    filament_plan: { slots: plan, force_colour_match: false },
    choices: printChoices,
    ...(copies === null ? {} : { copies }),
    ...plateFields(plate),
    ...(folderId !== null ? { folder_id: folderId } : projectId === undefined ? {} : { project_id: projectId }),
    options,
    ...sliceOverrides(printSequence, supports, filamentProfiles, primeTower),
    rack_position: rackPosition,
    rack_algorithm: rackAlgorithm,
  }
}

/**
 * The print dialog's one write, the source's run (`POST /print/outputs/{id}/run` or
 * `POST /print/library/{file_id}/run`, #313). A 422 is the resolver
 * refusing a combination; its `detail` is `runError`, and `refused` keeps Print disabled
 * until one of the choices changes. A run whose answer never arrived is `unanswered`
 * instead (#470). `reset` clears the run for the dialog's next open.
 */
export function useRunPrint({
  source,
  choices,
  printerId,
  registryPrinter = null,
  selection,
  plan,
  planChanged,
  copies,
  projectId,
  folderId = null,
  options,
  printSequence,
  supports = null,
  primeTower = null,
  filamentProfiles = NONE,
  rackPosition,
  rackAlgorithm,
  onRan,
}: RunInput) {
  const { nozzles, tier, processName, bedType, overrides, plate } = selection
  const platesKey = plateKey(plate)
  // By value: the caller builds a new array each render.
  const profilesKey = filamentProfiles.join(',')
  const supportsKey = JSON.stringify(supports)
  const towerKey = JSON.stringify(primeTower)
  const [running, setRunning] = useState(false)
  /** A refused run, shown above Print. */
  const [runError, setRunError] = useState<string | null>(null)
  /** Only a 422 — these choices cannot resolve — keeps Print disabled until one changes. */
  const [refused, setRefused] = useState(false)
  const [result, setResult] = useState<PrintRunResult | null>(null)
  /**
   * #470 — a run whose answer never arrived (a proxy's timeout, a dropped connection),
   * or one that failed after it had tried to queue (`may_have_queued`): Bambuddy may
   * have queued it anyway, so the dialog says so instead of offering Print again.
   * Closing the dialog is the way back to it.
   */
  const [unanswered, setUnanswered] = useState<string | null>(null)
  /** Which `run()` may still update the dialog: bumped by each run. */
  const runAttempt = useRef(0)
  /** Stops following the current run: the dialog unmounted, or was reset. */
  const following = useRef<AbortController | null>(null)

  useEffect(() => () => following.current?.abort(), [])

  // A refused run was refused for *these* choices; any change is worth another try.
  useEffect(() => {
    setRunError(null)
    setRefused(false)
  }, [
    nozzles,
    tier,
    processName,
    bedType,
    plan,
    overrides,
    printerId,
    platesKey,
    printSequence,
    supportsKey,
    towerKey,
    profilesKey,
    rackPosition,
    rackAlgorithm,
    projectId,
    folderId,
  ])

  /**
   * #78 / spec §7 — what this model reopens on next time: the printer, the nozzles,
   * tier and process, the print sequence (#4970), and the spools where they differ from
   * the suggestion (re-sending the suggestion is not a choice). Best effort, and only once
   * the print has started.
   */
  function rememberChoices() {
    const last = choices?.model_choices
    const next = {
      printer_id: printerId,
      // #4971 — a LAN-only printer is remembered by its registry id.
      printer: printerId === null ? registryPrinter : null,
      filament_plan: planChanged ? plan : [],
      nozzles,
      tier,
      process_name: processName,
      print_sequence: printSequence,
    }
    const before = {
      printer_id: last?.printer_id ?? null,
      printer: last?.printer ?? null,
      filament_plan: last?.filament_plan ?? [],
      nozzles: last?.nozzles ?? [],
      tier: last?.tier ?? null,
      process_name: last?.process_name ?? null,
      print_sequence: last?.print_sequence ?? null,
    }
    if (JSON.stringify(next) === JSON.stringify(before)) return
    if (source) void sourceApi(source).remember(next).catch(() => undefined)
  }

  /**
   * #83 — the plate this printer now has on it, the fallback when it has no archives. A
   * LAN-only printer has no Bambuddy id, so its registry id names it (#4971).
   */
  function rememberBedType() {
    const printer = printerId ?? registryPrinter
    if (printer === null || bedType === null) return
    void api.putPrinterBedType(printer, bedType).catch(() => undefined)
  }

  async function run() {
    const printChoices = printChoicesOf(selection)
    if (!source || !choices || !printChoices) return
    const attempt = ++runAttempt.current
    following.current?.abort()
    const controller = new AbortController()
    following.current = controller
    setRunning(true)
    setRunError(null)
    setRefused(false)
    try {
      const body: RegistryRunRequest = {
        ...printRunBody({
          printerId,
          registryPrinter,
          plan,
          printChoices,
          copies,
          plate,
          projectId,
          folderId,
          options,
          printSequence,
          supports,
          primeTower,
          filamentProfiles,
          rackPosition,
          rackAlgorithm,
        }),
        // One per press: the same choices printed again are a new print, while
        // runPrint's own retries of this press re-attach to its run (#470).
        request_id: newRequestId(),
      }
      // The run's POST, retries included, is the action's child; the polls are not (traceAction).
      const ran = await traceAction('print', printAttributes(source, body), (within) =>
        sourceApi(source).run(body, controller.signal, within),
      )
      if (attempt !== runAttempt.current) return
      setResult(ran)
      onRan(ran)
      rememberChoices()
      rememberBedType()
    } catch (cause) {
      if (attempt !== runAttempt.current || controller.signal.aborted) return
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

  function reset() {
    following.current?.abort()
    setRunError(null)
    setRefused(false)
    setResult(null)
    setUnanswered(null)
  }

  return { run, running, runError, refused, result, unanswered, reset }
}
