import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api, ApiError } from '../api/client'
import { choicesView, queuedResult } from '../mocks/choices'
import { DEFAULT_NOZZLES, type PrintSelection } from './usePrintChoices'
import { useRunPrint } from './useRunPrint'

const OUTPUT = 'a'.repeat(32)

const selection: PrintSelection = {
  nozzles: DEFAULT_NOZZLES,
  tier: 'standard',
  processName: null,
  bedType: 'Textured PEI Plate',
  overrides: {},
  plate: 'all',
}

/** The props, built once per render set: a fresh `plan` or `overrides` is itself a change. */
function input(overrides: Partial<PrintSelection> = {}) {
  return {
    outputId: OUTPUT,
    slug: 'name-keychain',
    choices: choicesView,
    printerId: 1,
    selection: { ...selection, ...overrides },
    plan: [],
    planChanged: false,
    copies: null,
    projectId: null,
    options: {},
    onRan: vi.fn(),
  }
}

describe('useRunPrint', () => {
  let runPrint: MockInstance<typeof api.runPrint>

  beforeEach(() => {
    runPrint = vi.spyOn(api, 'runPrint').mockResolvedValue(queuedResult)
    vi.spyOn(api, 'putModelChoices').mockResolvedValue(undefined as never)
    vi.spyOn(api, 'putPrinterBedType').mockResolvedValue(undefined as never)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('sends the selection, leaving an unset quantity to the remembered one', async () => {
    const props = input()
    const { result } = renderHook(() => useRunPrint(props))
    await act(() => result.current.run())

    const body = runPrint.mock.calls[0]?.[1]
    expect(body).toMatchObject({ printer_id: 1, plate_id: 1, all_plates: true })
    expect(body?.choices).toMatchObject({ tier: 'standard', bed_type: 'Textured PEI Plate' })
    expect(body).not.toHaveProperty('copies')
    expect(result.current.result).toEqual(queuedResult)
    expect(props.onRan).toHaveBeenCalledWith(queuedResult)
  })

  it('a 422 refuses these choices until one of them changes', async () => {
    runPrint.mockRejectedValueOnce(new ApiError(422, 'No process for 0.2 mm.'))
    const { result, rerender } = renderHook((props) => useRunPrint(props), {
      initialProps: input(),
    })
    await act(() => result.current.run())
    expect(result.current.runError).toBe('No process for 0.2 mm.')
    expect(result.current.refused).toBe(true)

    rerender(input({ tier: 'fine' }))
    await waitFor(() => expect(result.current.refused).toBe(false))
    expect(result.current.runError).toBeNull()
  })

  it('anything but a 422 is worth retrying as it stands', async () => {
    runPrint.mockRejectedValueOnce(new ApiError(502, 'Bambuddy is down.'))
    const props = input()
    const { result } = renderHook(() => useRunPrint(props))
    await act(() => result.current.run())
    expect(result.current.runError).toBe('Bambuddy is down.')
    expect(result.current.refused).toBe(false)
  })
})
