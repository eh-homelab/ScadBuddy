import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api, ApiError } from '../api/client'
import type { ChoicesView, SlotChoice } from '../api/types'
import { choicesView } from '../mocks/choices'
import { filamentOptions } from '../mocks/fixtures'
import type { PrintSource } from './printSource'
import { useFilamentPlan } from './useFilamentPlan'

const OUTPUT_ID = 'a'.repeat(32)
const OUTPUT: PrintSource = { kind: 'output', output: { id: OUTPUT_ID, slug: 'name-keychain' } }

describe('useFilamentPlan', () => {
  let getFilaments: MockInstance<typeof api.getFilaments>

  beforeEach(() => {
    getFilaments = vi.spyOn(api, 'getFilaments').mockResolvedValue(filamentOptions)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('reads nothing without choices or an output', () => {
    const { result } = renderHook(() => useFilamentPlan(undefined, null, 1))
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
    expect(getFilaments).not.toHaveBeenCalled()
  })

  it('is never ready on a plan seeded from other choices (#2186)', async () => {
    const next: ChoicesView = {
      ...choicesView,
      filaments: { ...choicesView.filaments, suggested: [{ slot_id: 1, spool_id: 22 }] },
    }
    const renders: { choices: ChoicesView | null; ready: boolean; plan: SlotChoice[] }[] = []
    const { result, rerender } = renderHook(
      ({ choices }: { choices: ChoicesView | null }) => {
        const state = useFilamentPlan(OUTPUT, choices, 1)
        renders.push({ choices, ready: state.planReady, plan: state.plan })
        return state
      },
      { initialProps: { choices: choicesView as ChoicesView | null } },
    )
    await waitFor(() => expect(result.current.planReady).toBe(true))
    rerender({ choices: null })
    expect(result.current.planReady).toBe(false)
    rerender({ choices: next })
    await waitFor(() => expect(result.current.planReady).toBe(true))

    expect(result.current.plan).toEqual(next.filaments.suggested)
    for (const render of renders.filter((entry) => entry.ready)) {
      expect(render.plan).toEqual(render.choices?.filaments.suggested)
    }
  })

  it('seeds plate 1 from the filaments the choices read already carried, no extra fetch', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 1))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(getFilaments).not.toHaveBeenCalled()
    expect(result.current.plan).toEqual(choicesView.filaments.suggested)
    expect(result.current.planChanged).toBe(false)
  })

  it('reads another plate’s filaments and seeds from those', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 2))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(getFilaments).toHaveBeenCalledWith(OUTPUT_ID, {
      printerId: choicesView.printer_id,
      plateId: 2,
    })
    expect(result.current.plan).toEqual(filamentOptions.suggested)
  })

  it('"all plates" reads every plate’s slots at once', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 'all'))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(getFilaments).toHaveBeenCalledWith(OUTPUT_ID, {
      printerId: choicesView.printer_id,
      allPlates: true,
    })
  })

  it('setPlan updates a slot, and planChanged tracks whether it still matches the suggestion', async () => {
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 1))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(result.current.planChanged).toBe(false)

    act(() => result.current.setPlan([{ slot_id: 1, spool_id: 9 }]))
    expect(result.current.planChanged).toBe(true)

    act(() => result.current.setPlan(choicesView.filaments.suggested as SlotChoice[]))
    expect(result.current.planChanged).toBe(false)
  })

  it('a source (output) change resets the plan while the new one loads', async () => {
    const { result, rerender } = renderHook(
      ({ source, choices }: { source: PrintSource; choices: ChoicesView | null }) =>
        useFilamentPlan(source, choices, 1),
      { initialProps: { source: OUTPUT, choices: choicesView as ChoicesView | null } },
    )
    await waitFor(() => expect(result.current.filaments).not.toBeNull())

    rerender({ source: OUTPUT, choices: null })
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
  })

  it('a plate change keeps the spools already picked (#1044)', async () => {
    const { result, rerender } = renderHook(
      ({ plate }: { plate: number | 'all' }) => useFilamentPlan(OUTPUT, choicesView, plate),
      { initialProps: { plate: 1 as number | 'all' } },
    )
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    const picks = [
      { slot_id: 1, spool_id: 22 },
      { slot_id: 2, spool_id: 24 },
    ]
    act(() => result.current.setPlan(picks))

    rerender({ plate: 2 })
    await waitFor(() => expect(getFilaments).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(result.current.plan).toEqual(picks)

    rerender({ plate: 'all' })
    await waitFor(() => expect(getFilaments).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    expect(result.current.plan).toEqual(picks)
  })

  it('a plate change seeds only slots new to it, and a pick survives a plate that lacks its slot (#1044)', async () => {
    const plateTwo = {
      ...filamentOptions,
      slots: (filamentOptions.slots ?? []).slice(0, 1),
      suggested: [{ slot_id: 1, spool_id: 21 }],
    }
    const { result, rerender } = renderHook(
      ({ plate }: { plate: number | 'all' }) => useFilamentPlan(OUTPUT, choicesView, plate),
      { initialProps: { plate: 1 as number | 'all' } },
    )
    await waitFor(() => expect(result.current.filaments).not.toBeNull())
    act(() =>
      result.current.setPlan([
        { slot_id: 1, spool_id: 22 },
        { slot_id: 2, spool_id: 24 },
      ]),
    )

    getFilaments.mockResolvedValueOnce(plateTwo)
    rerender({ plate: 2 })
    await waitFor(() => expect(result.current.plan).toEqual([{ slot_id: 1, spool_id: 22 }]))

    // Back to every plate: slot 2's pick comes back rather than the suggestion.
    rerender({ plate: 'all' })
    await waitFor(() =>
      expect(result.current.plan).toEqual([
        { slot_id: 1, spool_id: 22 },
        { slot_id: 2, spool_id: 24 },
      ]),
    )
  })

  it('a slot new to a later plate is seeded from the remembered plan, not the auto-match (#1044 review)', async () => {
    const plateOne = {
      ...filamentOptions,
      slots: (filamentOptions.slots ?? []).slice(0, 1),
      suggested: [{ slot_id: 1, spool_id: 21 }],
    }
    const remembering: ChoicesView = {
      ...choicesView,
      filaments: plateOne,
      model_choices: { printer_id: null, filament_plan: [{ slot_id: 2, spool_id: 27 }] },
    }
    // Every plate's slots, with an auto-match for slot 2 other than the remembered spool.
    getFilaments.mockResolvedValueOnce({
      ...filamentOptions,
      suggested: [
        { slot_id: 1, spool_id: 21 },
        { slot_id: 2, spool_id: 22 },
      ],
    })
    const { result, rerender } = renderHook(
      ({ plate }: { plate: number | 'all' }) => useFilamentPlan(OUTPUT, remembering, plate),
      { initialProps: { plate: 1 as number | 'all' } },
    )
    await waitFor(() => expect(result.current.plan).toEqual([{ slot_id: 1, spool_id: 21 }]))

    rerender({ plate: 'all' })
    await waitFor(() =>
      expect(result.current.plan).toEqual([
        { slot_id: 1, spool_id: 21 },
        { slot_id: 2, spool_id: 27 },
      ]),
    )
  })

  it('a failed read reports the error and clears the plan', async () => {
    getFilaments.mockRejectedValueOnce(new ApiError(503, 'Bambuddy is down.'))
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 2))
    await waitFor(() => expect(result.current.filamentError).toBe('Bambuddy is down.'))
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
  })
})
