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
  return { errors: [error], warnings: [], rack: null }
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
    expect(check).toHaveBeenCalledWith(OUTPUT.id, request('0.4'), false)
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
    expect(library).toHaveBeenCalledWith(89, request('0.4'), false)
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

  it("drops the process's values once the choices change, until the new check answers (#5627)", async () => {
    const parts = (top: number): PrintCheck => ({
      errors: [],
      warnings: [],
      rack: null,
      process_part_settings: { top_shell_layers: top },
    })
    check.mockResolvedValueOnce(parts(5)).mockResolvedValueOnce(parts(5)).mockResolvedValueOnce(parts(7))
    const { result, rerender } = renderHook(
      ({ size, top }: { size: Size; top?: number }) =>
        usePrintCheck(
          SOURCE,
          { ...request(size), ...(top === undefined ? {} : { part_settings: { '1': { top_shell_layers: top } } }) },
          true,
        ),
      { initialProps: { size: '0.4' } as { size: Size; top?: number } },
    )
    await settle()
    expect(result.current.processValuesRead).toBe(true)
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })

    // A setting typed in changes only the request's part_settings: the values still hold.
    rerender({ size: '0.4', top: 9 })
    expect(result.current.processValuesRead).toBe(true)
    await settle(ANALYSIS_DEBOUNCE_MS)

    // Another nozzle is another process: nothing is shown until its check answers.
    rerender({ size: '0.2', top: 9 })
    expect(result.current.processValuesRead).toBe(false)
    expect(result.current.processPartSettings).toBeNull()
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(result.current.processValuesRead).toBe(true)
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 7 })
  })

  it("asks for the process's values only until they are read for this process (#5686)", async () => {
    const parts: PrintCheck = { errors: [], warnings: [], rack: null, process_part_settings: { top_shell_layers: 5 } }
    check.mockResolvedValue(parts)
    const { result, rerender } = renderHook(
      ({ size, top }: { size: Size; top?: number }) =>
        usePrintCheck(
          SOURCE,
          { ...request(size), ...(top === undefined ? {} : { part_settings: { '1': { top_shell_layers: top } } }) },
          true,
        ),
      { initialProps: { size: '0.4' } as { size: Size; top?: number } },
    )
    await settle()
    expect(check).toHaveBeenCalledTimes(1)
    expect(check.mock.calls[0]![2]).toBe(true)

    // A settled edit of the settings alone checks again, but does not read the preset again.
    rerender({ size: '0.4', top: 9 })
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(check).toHaveBeenCalledTimes(2)
    expect(check.mock.calls[1]![2]).toBe(false)
    expect(result.current.processValuesRead).toBe(true)
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })

    // Another process asks again.
    rerender({ size: '0.2', top: 9 })
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(check).toHaveBeenCalledTimes(3)
    expect(check.mock.calls[2]![2]).toBe(true)
  })

  it("asks again after a check that could not read the process's values (#5686)", async () => {
    check
      .mockResolvedValueOnce({ errors: [], warnings: [], rack: null, process_part_settings: null })
      .mockResolvedValueOnce({ errors: [], warnings: [], rack: null, process_part_settings: { top_shell_layers: 5 } })
    const { result, rerender } = renderHook(
      ({ top }: { top?: number }) =>
        usePrintCheck(
          SOURCE,
          { ...request('0.4'), ...(top === undefined ? {} : { part_settings: { '1': { top_shell_layers: top } } }) },
          true,
        ),
      { initialProps: {} as { top?: number } },
    )
    await settle()
    expect(check.mock.calls[0]![2]).toBe(true)
    expect(result.current.processPartSettings).toBeNull()

    // The same process, but its values are not known yet: the next check asks again.
    rerender({ top: 9 })
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(check).toHaveBeenCalledTimes(2)
    expect(check.mock.calls[1]![2]).toBe(true)
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })
  })

  it('keeps the values when a late answer for a source left behind lands (#5686)', async () => {
    check.mockResolvedValue({ errors: [], warnings: [], rack: null, process_part_settings: { top_shell_layers: 5 } })
    let answer!: (value: PrintCheck) => void
    vi.spyOn(api, 'checkLibraryPrint').mockReturnValue(new Promise((resolve) => (answer = resolve)))
    const { result, rerender } = renderHook(({ source }) => usePrintCheck(source, request('0.4'), true), {
      initialProps: { source: SOURCE as PrintSource },
    })
    await settle()
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })

    rerender({ source: OTHER })
    await settle()
    rerender({ source: SOURCE })
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })

    answer({ errors: [], warnings: [], rack: null, process_part_settings: { top_shell_layers: 9 } })
    await settle()
    expect(result.current.processValuesRead).toBe(true)
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })
  })

  it("keeps the process's values when a re-check fails, but never another process's (#5686)", async () => {
    const parts: PrintCheck = { errors: [], warnings: [], rack: null, process_part_settings: { top_shell_layers: 5 } }
    check.mockResolvedValueOnce(parts)
    const { result, rerender } = renderHook(
      ({ size, top }: { size: Size; top?: number }) =>
        usePrintCheck(
          SOURCE,
          { ...request(size), ...(top === undefined ? {} : { part_settings: { '1': { top_shell_layers: top } } }) },
          true,
        ),
      { initialProps: { size: '0.4' } as { size: Size; top?: number } },
    )
    await settle()
    expect(result.current.processValuesRead).toBe(true)

    // The check after an edit fails: the controls stay enabled on the values already read.
    check.mockRejectedValueOnce(new ApiError(502, 'Bambuddy did not answer'))
    rerender({ size: '0.4', top: 9 })
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.verdict).toBeNull()
    expect(result.current.processValuesRead).toBe(true)
    expect(result.current.processPartSettings).toEqual({ top_shell_layers: 5 })

    // Another process whose check fails has no values: the old process's are not used.
    check.mockRejectedValueOnce(new ApiError(502, 'Bambuddy did not answer'))
    rerender({ size: '0.2', top: 9 })
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.processValuesRead).toBe(false)
    expect(result.current.processPartSettings).toBeNull()
  })
})
