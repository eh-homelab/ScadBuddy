import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import type { ChoicesView, NozzleChoice, OutputPlate, PresetRef, PrintChoices } from '../api/types'
import { DEFAULT_NOZZLES, refKey } from './printChoices'
import { sourceApi, sourceKey, type PrintSource } from './printSource'
import { useLatest } from './useLatest'

export type Tier = NonNullable<PrintChoices['tier']>

/** What the run sends of the choices made over the read (the spools aside). */
export interface PrintSelection {
  nozzles: NozzleChoice[]
  tier: Tier | null
  processName: string | null
  bedType: string | null
  /** Advanced only — a filament preset per slot id, in place of the spool's own. */
  overrides: Record<string, PresetRef>
  /** #83 — the plate (or all of them) to print. */
  plate: number | 'all'
}

/**
 * The print dialog's one read, the source's choices read, for the chosen printer,
 * and the choices made over it: the nozzles, tier or process, plate type, per-slot preset
 * overrides and which plate of the output (#83).
 *
 * `reload` re-reads for `askedPrinter` — `null`, so the server's remembered printer,
 * unless the user picked one — and is what the load effect itself runs.
 * `reset` puts the choices back as a fresh open finds them, for when the dialog closes.
 * `sourceKey` identifies what is being printed; what belongs to one source is reset when
 * it changes, here and by the caller's own effect keyed on it.
 */
export function usePrintChoices(open: boolean, source: PrintSource | undefined) {
  const key = sourceKey(source)
  const latest = useLatest(source)
  const [choices, setChoices] = useState<ChoicesView | null>(null)
  /** The printer asked for; `null` lets the server open on the remembered one. */
  const [askedPrinter, setAskedPrinter] = useState<number | null>(null)
  const [nozzles, setNozzles] = useState<NozzleChoice[]>(DEFAULT_NOZZLES)
  const [tier, setTier] = useState<Tier | null>('standard')
  const [processName, setProcessName] = useState<string | null>(null)
  const [bedType, setBedType] = useState<string | null>(null)
  const [advanced, setAdvanced] = useState(false)
  const [overrides, setOverrides] = useState<Record<string, PresetRef>>({})
  /** #83 — the output's plates, and the one (or all) to print. */
  const [plates, setPlates] = useState<OutputPlate[]>([])
  const [plate, setPlate] = useState<number | 'all'>(1)

  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  /**
   * Supersedes an in-flight read. The dialog is not unmounted when it closes, so a read
   * started for one output or printer can resolve after another has been asked for.
   */
  const attempt = useRef(0)
  /** The `attempt` of the read the current `choices` came from. */
  const [choicesRead, setChoicesRead] = useState(0)
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
  const reload = useCallback(() => {
    const current = latest.current
    if (!open || !current || sourceKey(current) !== key) return
    const token = (attempt.current += 1)
    setLoading(true)
    setLoadError(null)
    sourceApi(current)
      .getChoices(askedPrinter)
      .then((next) => {
        if (token !== attempt.current) return
        setChoices(next)
        setChoicesRead(token)
        // The server already applied last archive → remembered → default.
        setBedType(next.bed_type)
        if (!seeded.current) {
          seeded.current = true
          seedDialog(next.model_choices)
        }
      })
      .catch((cause: unknown) => {
        if (token !== attempt.current) return
        setChoices(null)
        setLoadError(cause instanceof ApiError ? cause.detail : 'Could not read the print choices.')
      })
      .finally(() => {
        if (token === attempt.current) setLoading(false)
      })
  }, [open, key, askedPrinter, latest])
  useEffect(() => {
    reload()
  }, [reload])

  // One output's plates and overrides do not survive a change of output. PrintPicker
  // resets its print options on the same `sourceKey`; the two are one reset.
  const resetKey = key
  useEffect(() => {
    setPlate(1)
    setPlates([])
    setOverrides({})
    seeded.current = false
  }, [resetKey])

  useEffect(() => {
    if (!open || !latest.current) return
    let live = true
    sourceApi(latest.current)
      .getPlates()
      .then((next) => live && setPlates(next))
      // One plate is what every ScadBuddy render is, so an unreadable list asks nothing.
      .catch(() => live && setPlates([]))
    return () => {
      live = false
    }
  }, [open, key, latest])

  const printerId = choices?.printer_id ?? null
  const printers = choices?.printers ?? []
  const printer = printers.find((entry) => entry.id === printerId)
  const size = nozzles[0]?.size ?? '0.4'

  function reset() {
    setAskedPrinter(null)
    setNozzles(DEFAULT_NOZZLES)
    setTier('standard')
    setProcessName(null)
    setAdvanced(false)
    setOverrides({})
    seeded.current = false
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

  function changeQuality(next: { tier: Tier | null; processName: string | null }) {
    setTier(next.tier)
    setProcessName(next.processName)
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

  const selection: PrintSelection = { nozzles, tier, processName, bedType, overrides, plate }
  return {
    sourceKey: resetKey,
    choices,
    /** The read `choices` came from, comparable with `readsStarted`. */
    choicesRead,
    /** How many choices reads have started: a read numbered above it starts later. */
    readsStarted: attempt,
    loading,
    loadError,
    reload,
    printers,
    printerId,
    printer,
    askPrinter: setAskedPrinter,
    plates,
    selection,
    size,
    advanced,
    changeNozzles,
    changeQuality,
    setBedType,
    setOverride,
    setPlate,
    toggleAdvanced,
    reset,
  }
}
