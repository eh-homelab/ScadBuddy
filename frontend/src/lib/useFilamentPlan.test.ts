import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api, ApiError } from '../api/client'
import type { ChoicesView, FilamentOptions, SlotChoice } from '../api/types'
import { choicesView } from '../mocks/choices'
import { filamentOptions } from '../mocks/fixtures'
import type { PrintSource } from './printSource'
import { useFilamentPlan } from './useFilamentPlan'

const OUTPUT_ID = 'a'.repeat(32)
const OUTPUT: PrintSource = { kind: 'output', output: { id: OUTPUT_ID, slug: 'name-keychain' } }

/**
 * A right/left-wired H2C (no Filament Track Switch): the 0.2 mounts on the right, the
 * 0.4 on the left, so a slot's spool depends on the nozzle size actually chosen. Mirrors
 * `filaments.test.ts`'s `fitPlan` fixture.
 */
const wired: FilamentOptions = {
  library_file_id: 8812,
  printer_id: 1,
  nozzles: [
    { nozzle_type: 'HS00', nozzle_diameter: '0.2' },
    { nozzle_type: 'HH01', nozzle_diameter: '0.4' },
  ],
  track_switch: false,
  slots: [{ slot_id: 1, material: 'PLA', colour: '#0047BB', used_grams: null }],
  spools: [
    { spool_id: 1, material: 'PLA', colour: '#0047BB', extruder: 0, side: 'R', loaded: null, remaining_g: 800, storage_location: null, brand: null, subtype: null, color_name: null, slicer_filament: null, slicer_filament_name: null },
    { spool_id: 2, material: 'PLA', colour: '#0A4FC0', extruder: 1, side: 'L', loaded: null, remaining_g: 800, storage_location: null, brand: null, subtype: null, color_name: null, slicer_filament: null, slicer_filament_name: null },
  ],
  suggested: [{ slot_id: 1, spool_id: 1 }],
  warnings: [],
}

describe('useFilamentPlan', () => {
  let getFilaments: MockInstance<typeof api.getFilaments>

  beforeEach(() => {
    getFilaments = vi.spyOn(api, 'getFilaments').mockResolvedValue(filamentOptions)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('reads nothing without choices or an output', () => {
    const { result } = renderHook(() => useFilamentPlan(undefined, null, 1, '0.4'))
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
    expect(getFilaments).not.toHaveBeenCalled()
  })

  it('seeds plate 1 from the filaments the choices read already carried, no extra fetch', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 1, '0.4'))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(getFilaments).not.toHaveBeenCalled()
    expect(result.current.plan).toEqual(choicesView.filaments.suggested)
    expect(result.current.planChanged).toBe(false)
  })

  it('reads another plate’s filaments and seeds from those', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 2, '0.4'))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(getFilaments).toHaveBeenCalledWith(OUTPUT_ID, {
      printerId: choicesView.printer_id,
      plateId: 2,
    })
    expect(result.current.plan).toEqual(filamentOptions.suggested)
  })

  it('"all plates" reads every plate’s slots at once', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 'all', '0.4'))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(getFilaments).toHaveBeenCalledWith(OUTPUT_ID, {
      printerId: choicesView.printer_id,
      allPlates: true,
    })
  })

  it('setPlan updates a slot, and planChanged tracks whether it still matches the suggestion', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 1, '0.4'))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(result.current.planChanged).toBe(false)

    act(() => result.current.setPlan([{ slot_id: 1, spool_id: 9 }]))
    expect(result.current.planChanged).toBe(true)

    act(() => result.current.setPlan(choicesView.filaments.suggested as SlotChoice[]))
    expect(result.current.planChanged).toBe(false)
  })

  it('a nozzle-size change refits the plan, swapping a spool the new size rules out', async () => {
    getFilaments.mockResolvedValue(wired)
    const { result, rerender } = renderHook(
      ({ size }: { size: string }) => useFilamentPlan(OUTPUT, choicesView, 2, size),
      { initialProps: { size: '0.2' } },
    )
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    // At 0.2 only the right prints: the suggestion (spool 1, on the right) stands.
    expect(result.current.plan).toEqual([{ slot_id: 1, spool_id: 1 }])

    rerender({ size: '0.4' })
    // At 0.4 only the left prints, so spool 1 is swapped for the near-blue spool 2.
    await waitFor(() => expect(result.current.plan).toEqual([{ slot_id: 1, spool_id: 2 }]))
  })

  it('a source (output) change resets the plan while the new one loads', async () => {
    const { result, rerender } = renderHook(
      ({ source, choices }: { source: PrintSource; choices: ChoicesView | null }) =>
        useFilamentPlan(source, choices, 1, '0.4'),
      { initialProps: { source: OUTPUT, choices: choicesView as ChoicesView | null } },
    )
    await waitFor(() => expect(result.current.filaments).not.toBeNull())

    rerender({ source: OUTPUT, choices: null })
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
  })

  it('a failed read reports the error and clears the plan', async () => {
    getFilaments.mockRejectedValueOnce(new ApiError(503, 'Bambuddy is down.'))
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 2, '0.4'))
    await waitFor(() => expect(result.current.filamentError).toBe('Bambuddy is down.'))
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
  })
})
