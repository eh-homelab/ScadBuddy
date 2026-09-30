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

  it('a failed read reports the error and clears the plan', async () => {
    getFilaments.mockRejectedValueOnce(new ApiError(503, 'Bambuddy is down.'))
    const { result } = renderHook(() => useFilamentPlan(OUTPUT, choicesView, 2))
    await waitFor(() => expect(result.current.filamentError).toBe('Bambuddy is down.'))
    expect(result.current.filaments).toBeNull()
    expect(result.current.plan).toEqual([])
  })
})
