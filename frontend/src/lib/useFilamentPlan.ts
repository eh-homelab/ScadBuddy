import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import type { ChoicesView, FilamentOptions, SlotChoice } from '../api/types'
import { seedPlan } from './filaments'
import { sourceApi, sourceKey, type PrintSource } from './printSource'
import { useLatest } from './useLatest'

/**
 * #87 — the inventory behind the filament picker, and the plan built on it, for the plate
 * (or all plates) chosen of a source whose choices have been read.
 *
 * `planChanged` is whether the plan differs from the server's suggestion, which is what
 * decides whether it is remembered for the model (#78).
 */
export function useFilamentPlan(
  source: PrintSource | undefined,
  choices: ChoicesView | null,
  plate: number | 'all',
) {
  const key = sourceKey(source)
  const latest = useLatest(source)
  const [filaments, setFilaments] = useState<FilamentOptions | null>(null)
  const [plan, setPlan] = useState<SlotChoice[]>([])
  const [filamentError, setFilamentError] = useState<string | null>(null)

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
    const current = latest.current
    if (!choices || !current) {
      setFilaments(null)
      setPlan([])
      return
    }
    const seed = (next: FilamentOptions) => {
      setFilaments(next)
      // What this model last printed with seeds the selection, else the server's
      // auto-match (#78); every slot stays editable.
      setPlan(seedPlan(next, JSON.parse(rememberedPlan) as SlotChoice[]))
    }
    if (chosenPlate === 1 && !allPlates) {
      seed(choices.filaments)
      return
    }
    sourceApi(current)
      .getFilaments(
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
  }, [choices, key, latest, chosenPlate, allPlates, rememberedPlan])

  const suggested = filaments?.suggested ?? []
  const planChanged =
    plan.length !== suggested.length ||
    suggested.some(
      (choice) =>
        plan.find((entry) => entry.slot_id === choice.slot_id)?.spool_id !== choice.spool_id,
    )

  return { filaments, plan, setPlan, planChanged, filamentError }
}
