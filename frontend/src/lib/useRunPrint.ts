import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type {
  ChoicesView,
  PrintOptions,
  PrintRunRequest,
  PrintRunResult,
  SlotChoice,
} from '../api/types'
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
 * until one of the choices changes. `reset` clears the run for the dialog's next open.
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
    if (!outputId || !choices || bedType === null) return
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
        plate_id: plate === 'all' ? 1 : plate,
        all_plates: plate === 'all',
        project_id: projectId,
        options,
      }
      const ran = await api.runPrint(outputId, body)
      setResult(ran)
      onRan(ran)
      rememberChoices()
      rememberBedType()
    } catch (cause) {
      setRunError(cause instanceof ApiError ? cause.detail : 'The print could not be started.')
      // Anything else (Bambuddy down, a timeout) is worth retrying as it stands.
      setRefused(cause instanceof ApiError && cause.status === 422)
    } finally {
      setRunning(false)
    }
  }

  function reset() {
    setRunError(null)
    setRefused(false)
    setResult(null)
  }

  return { run, running, runError, refused, result, reset }
}
