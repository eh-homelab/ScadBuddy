import { useEffect, useRef, useState } from 'react'
import { api, ApiError, mayHaveRun, newRequestId } from '../api/client'
import type {
  ChoicesView,
  PrintOptions,
  PrintRunRequest,
  PrintRunResult,
  SlotChoice,
} from '../api/types'
import { printChoicesOf } from './printChoices'
import type { PrintSelection } from './usePrintChoices'

interface RunInput {
  outputId: string | undefined
  slug: string
  choices: ChoicesView | null
  printerId: number | null
  selection: PrintSelection
  plan: SlotChoice[]
  /** Whether `plan` differs from the suggestion, so it is worth remembering (#78). */
  planChanged: boolean
  /** `null` leaves the quantity to the remembered options (#124). */
  copies: number | null
  /** #79 — the Bambuddy project this print is filed under. */
  projectId: number | null
  /** #88 — this print's overrides, all but `quantity`, which is `copies`. */
  options: PrintOptions
  onRan: (result: PrintRunResult) => void
}

/**
 * The print dialog's one write, `POST /print/outputs/{id}/run`. A 422 is the resolver
 * refusing a combination; its `detail` is `runError`, and `refused` keeps Print disabled
 * until one of the choices changes. A run whose answer never arrived is `unanswered`
 * instead (#470). `reset` clears the run for the dialog's next open.
 */
export function useRunPrint({
  outputId,
  slug,
  choices,
  printerId,
  selection,
  plan,
  planChanged,
  copies,
  projectId,
  options,
  onRan,
}: RunInput) {
  const { nozzles, tier, processName, bedType, overrides, plate } = selection
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

  // A refused run was refused for *these* choices; any change is worth another try.
  useEffect(() => {
    setRunError(null)
    setRefused(false)
  }, [nozzles, tier, processName, bedType, plan, overrides, printerId, plate])

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
    const printChoices = printChoicesOf(selection)
    if (!outputId || !choices || !printChoices) return
    const attempt = ++runAttempt.current
    setRunning(true)
    setRunError(null)
    setRefused(false)
    try {
      const body: PrintRunRequest = {
        printer_id: printerId,
        filament_plan: { slots: plan, force_colour_match: false },
        choices: printChoices,
        ...(copies === null ? {} : { copies }),
        plate_id: plate === 'all' ? 1 : plate,
        all_plates: plate === 'all',
        project_id: projectId,
        options,
        // One per press: the same choices printed again are a new print, while
        // runPrint's own retries of this press re-attach to its run (#470).
        request_id: newRequestId(),
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

  function reset() {
    setRunError(null)
    setRefused(false)
    setResult(null)
    setUnanswered(null)
  }

  return { run, running, runError, refused, result, unanswered, reset }
}
