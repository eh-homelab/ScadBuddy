import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api, ApiError } from '../api/client'
import type { ChoicesView } from '../api/types'
import { choicesView } from '../mocks/choices'
import { DEFAULT_NOZZLES } from './printChoices'
import { usePrintChoices } from './usePrintChoices'

const OUTPUT_A = 'a'.repeat(32)
const OUTPUT_B = 'c'.repeat(32)

/** A model that last printed on a 0.2 nozzle with a named process. */
const remembered: ChoicesView = {
  ...choicesView,
  model_choices: {
    printer_id: 1,
    filament_plan: [],
    nozzles: [
      { size: '0.2', flow: 'standard' },
      { size: '0.2', flow: 'standard' },
    ],
    tier: null,
    process_name: '0.10mm Standard @BBL H2C 0.2 nozzle',
  },
}

describe('usePrintChoices', () => {
  let read: MockInstance<typeof api.getChoices>

  beforeEach(() => {
    read = vi.spyOn(api, 'getChoices').mockResolvedValue(remembered)
    vi.spyOn(api, 'getOutputPlates').mockResolvedValue([])
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('reads nothing while closed', () => {
    renderHook(() => usePrintChoices(false, { kind: 'output', output: { id: OUTPUT_A, slug: 'm' } }))
    expect(read).not.toHaveBeenCalled()
  })

  it('seeds the last choices once, and a read for another printer keeps the edits', async () => {
    const { result } = renderHook(() => usePrintChoices(true, { kind: 'output', output: { id: OUTPUT_A, slug: 'm' } }))
    await waitFor(() => expect(result.current.choices).not.toBeNull())
    expect(result.current.selection.nozzles[0]?.size).toBe('0.2')
    expect(result.current.selection.processName).toBe(remembered.model_choices?.process_name)
    expect(result.current.advanced).toBe(true)

    act(() => result.current.changeNozzles(DEFAULT_NOZZLES))
    act(() => result.current.askPrinter(2))
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(OUTPUT_A, 2))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.selection.nozzles).toEqual(DEFAULT_NOZZLES)
  })

  it('reload re-reads for the printer in view and clears a failed read', async () => {
    read.mockRejectedValueOnce(new ApiError(503, 'Bambuddy is down.'))
    const { result } = renderHook(() => usePrintChoices(true, { kind: 'output', output: { id: OUTPUT_A, slug: 'm' } }))
    await waitFor(() => expect(result.current.loadError).toBe('Bambuddy is down.'))
    expect(result.current.choices).toBeNull()

    act(() => result.current.reload())
    await waitFor(() => expect(result.current.choices).not.toBeNull())
    expect(result.current.loadError).toBeNull()
    expect(read).toHaveBeenCalledTimes(2)
    expect(read).toHaveBeenLastCalledWith(OUTPUT_A, null)
  })

  it('a new output resets the plate and seeds again', async () => {
    const { result, rerender } = renderHook(({ id }) => usePrintChoices(true, { kind: 'output', output: { id, slug: 'm' } }), {
      initialProps: { id: OUTPUT_A },
    })
    await waitFor(() => expect(result.current.choices).not.toBeNull())
    act(() => result.current.setPlate('all'))
    act(() => result.current.changeNozzles(DEFAULT_NOZZLES))

    rerender({ id: OUTPUT_B })
    expect(result.current.selection.plate).toBe(1)
    await waitFor(() => expect(read).toHaveBeenLastCalledWith(OUTPUT_B, null))
    await waitFor(() => expect(result.current.selection.nozzles[0]?.size).toBe('0.2'))
  })

  it('reset puts the choices back as a fresh open finds them', async () => {
    const { result } = renderHook(() => usePrintChoices(true, { kind: 'output', output: { id: OUTPUT_A, slug: 'm' } }))
    await waitFor(() => expect(result.current.advanced).toBe(true))
    act(() => result.current.reset())
    expect(result.current.selection.nozzles).toEqual(DEFAULT_NOZZLES)
    expect(result.current.selection.tier).toBe('standard')
    expect(result.current.selection.processName).toBeNull()
    expect(result.current.advanced).toBe(false)
  })
})
