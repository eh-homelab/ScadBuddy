import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api, ApiError } from '../api/client'
import type { PrintCheck, PrintRunRequest } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import type { PrintSource } from './printSource'
import { ANALYSIS_DEBOUNCE_MS } from './useAnalysis'
import { usePrintCheck } from './usePrintCheck'

const OUTPUT = fixtures.outputs[0]!
const SOURCE: PrintSource = { kind: 'output', output: OUTPUT }
const OTHER: PrintSource = { kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } }

type Size = PrintRunRequest['choices']['nozzles'][number]['size']

function request(size: Size): PrintRunRequest {
  return {
    printer_id: 1,
    filament_plan: { slots: [], force_colour_match: false },
    choices: { nozzles: [{ size, flow: 'standard' }], tier: 'standard', bed_type: 'Textured PEI Plate' },
    plate_id: 1,
    all_plates: false,
  } as unknown as PrintRunRequest
}

function verdict(error: string): PrintCheck {
  return { errors: [error], warnings: [] }
}

async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('usePrintCheck', () => {
  let check: MockInstance<typeof api.checkPrint>

  beforeEach(() => {
    vi.useFakeTimers()
    check = vi.spyOn(api, 'checkPrint')
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('reads the verdict for the request, and says it is current', async () => {
    check.mockResolvedValue(verdict('refused'))
    const { result } = renderHook(() => usePrintCheck(SOURCE, request('0.4')))
    await settle()
    expect(check).toHaveBeenCalledWith(OUTPUT.id, request('0.4'))
    expect(result.current.verdict).toEqual(verdict('refused'))
    expect(result.current.current).toBe(true)
    expect(result.current.error).toBeUndefined()
  })

  it('reads nothing until the dialog has a request', async () => {
    const { result } = renderHook(() => usePrintCheck(SOURCE, null))
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(check).not.toHaveBeenCalled()
    expect(result.current.verdict).toBeNull()
    expect(result.current.current).toBe(false)
  })

  it('keeps the last verdict while the choices settle, but not as current', async () => {
    check.mockResolvedValueOnce(verdict('first'))
    const { result, rerender } = renderHook(({ size }) => usePrintCheck(SOURCE, request(size)), {
      initialProps: { size: '0.4' as Size },
    })
    await settle()
    expect(result.current.verdict).toEqual(verdict('first'))

    let answer!: (value: PrintCheck) => void
    check.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)))
    rerender({ size: '0.2' })
    // The old verdict stays on screen, but no longer gates Print.
    expect(result.current.verdict).toEqual(verdict('first'))
    expect(result.current.current).toBe(false)

    await settle(ANALYSIS_DEBOUNCE_MS - 1)
    expect(check).toHaveBeenCalledTimes(1)
    await settle(1)
    expect(check).toHaveBeenCalledTimes(2)
    expect(result.current.current).toBe(false)

    answer(verdict('second'))
    await settle()
    expect(result.current.verdict).toEqual(verdict('second'))
    expect(result.current.current).toBe(true)
  })

  it('drops a verdict read for another source', async () => {
    check.mockResolvedValue(verdict('output'))
    const library = vi.spyOn(api, 'checkLibraryPrint').mockReturnValue(new Promise(() => {}))
    const { result, rerender } = renderHook(({ source }) => usePrintCheck(source, request('0.4')), {
      initialProps: { source: SOURCE as PrintSource },
    })
    await settle()
    expect(result.current.verdict).toEqual(verdict('output'))

    rerender({ source: OTHER })
    expect(result.current.verdict).toBeNull()
    expect(result.current.current).toBe(false)
    await settle()
    expect(library).toHaveBeenCalledWith(89, request('0.4'))
  })

  it('reports a failed read as an error, with no verdict, and reads again on reload', async () => {
    check.mockResolvedValueOnce(verdict('first'))
    const { result, rerender } = renderHook(({ size }) => usePrintCheck(SOURCE, request(size)), {
      initialProps: { size: '0.4' as Size },
    })
    await settle()

    check.mockRejectedValueOnce(new ApiError(502, 'Bambuddy did not answer'))
    rerender({ size: '0.2' })
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.verdict).toBeNull()
    expect(result.current.current).toBe(false)

    check.mockResolvedValueOnce(verdict('again'))
    act(() => result.current.reload())
    await settle()
    expect(check).toHaveBeenCalledTimes(3)
    expect(result.current.error).toBeUndefined()
    expect(result.current.verdict).toEqual(verdict('again'))
    expect(result.current.current).toBe(true)
  })
})
