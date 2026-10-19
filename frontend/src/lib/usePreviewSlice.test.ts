import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { PreviewStarted, PrintRunRequest, SlicePreview } from '../api/types'
import type { PrintSource } from './printSource'
import { RESLICE_DEBOUNCE_MS, RESLICE_POLL_MS, usePreviewSlice } from './usePreviewSlice'

/** The native slicer's progress and its `?slicer` reads (plan 2026-10-16 Task 14). */

const source = { kind: 'output', output: { id: 'out-1', slug: 'demo' } } as PrintSource
const request = { printer_id: 1, copies: 1, filament_plan: { slots: [] }, choices: { nozzles: [] } } as unknown as PrintRunRequest

function running(progress: number | null, slicer: SlicePreview['slicer']): SlicePreview {
  return { job_id: 5, status: 'running', slots: [], progress, slicer, warnings: [] } as SlicePreview
}

function completed(slicer: SlicePreview['slicer']): SlicePreview {
  return { job_id: 5, status: 'completed', slots: [], print_time_seconds: 60, slicer, warnings: [] } as SlicePreview
}

function started(slicer: PreviewStarted['slicer']): PreviewStarted {
  return { job_id: 5, plate_id: 1, nozzle_plan: null, slicer }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('usePreviewSlice on the native slicer', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('follows the progress of each read while the slice runs', async () => {
    vi.spyOn(api, 'previewSlice').mockResolvedValue(started('native'))
    vi.spyOn(api, 'getPreviewSlice')
      .mockResolvedValueOnce(running(20, 'native'))
      .mockResolvedValueOnce(running(60, 'native'))
      .mockResolvedValueOnce(completed('native'))
    const { result } = renderHook(() => usePreviewSlice(source, request))

    await advance(RESLICE_DEBOUNCE_MS)
    expect(result.current.progress).toBe(20)
    await advance(RESLICE_POLL_MS)
    expect(result.current.progress).toBe(60)
    await advance(RESLICE_POLL_MS)
    expect(result.current.slicing).toBe(false)
    expect(result.current.progress).toBeNull()
  })

  it('reads a native start, and its toolpath, with slicer=native', async () => {
    vi.spyOn(api, 'previewSlice').mockResolvedValue(started('native'))
    const read = vi.spyOn(api, 'getPreviewSlice').mockResolvedValue(completed('native'))
    const { result } = renderHook(() => usePreviewSlice(source, request))

    await advance(RESLICE_DEBOUNCE_MS)

    expect(read).toHaveBeenCalledWith('out-1', 5, 'native')
    expect(result.current.toolpathUrl).toMatch(/\/preview-slices\/5\/toolpath\?slicer=native$/)
  })

  it('reads a library file\u2019s native slice with slicer=native too (Task 14/15 review M5)', async () => {
    const library = { kind: 'library', file: { id: 7, filename: 'a.3mf' } } as PrintSource
    vi.spyOn(api, 'previewLibrarySlice').mockResolvedValue(started('native'))
    const read = vi.spyOn(api, 'getLibraryPreviewSlice').mockResolvedValue(completed('native'))
    const { result } = renderHook(() => usePreviewSlice(library, request))

    await advance(RESLICE_DEBOUNCE_MS)

    expect(read).toHaveBeenCalledWith(7, 5, 'native')
    expect(result.current.toolpathUrl).toMatch(/\/print\/library\/7\/preview-slices\/5\/toolpath\?slicer=native$/)
  })

  it('drops the progress when the choices change mid-slice (Task 14/15 review M5)', async () => {
    vi.spyOn(api, 'previewSlice').mockResolvedValue(started('native'))
    vi.spyOn(api, 'getPreviewSlice')
      .mockResolvedValueOnce(running(40, 'native'))
      .mockResolvedValue(running(null, 'native'))
    const { result, rerender } = renderHook(({ wanted }) => usePreviewSlice(source, wanted), {
      initialProps: { wanted: request },
    })
    await advance(RESLICE_DEBOUNCE_MS)
    expect(result.current.progress).toBe(40)

    rerender({ wanted: { ...request, copies: 2 } as PrintRunRequest })
    await advance(0)

    expect(result.current.progress).toBeNull()
  })

  it('reads a Bambuddy start, and its toolpath, with no slicer at all', async () => {
    vi.spyOn(api, 'previewSlice').mockResolvedValue(started('bambuddy'))
    const read = vi.spyOn(api, 'getPreviewSlice').mockResolvedValue(completed('bambuddy'))
    const { result } = renderHook(() => usePreviewSlice(source, request))

    await advance(RESLICE_DEBOUNCE_MS)

    expect(read).toHaveBeenCalledWith('out-1', 5, 'bambuddy')
    expect(result.current.toolpathUrl).toMatch(/\/preview-slices\/5\/toolpath$/)
  })
})

describe('the preview reads', () => {
  it('ask for slicer=native only for a native slice', () => {
    expect(api.previewToolpathUrl('o', 3, 'native')).toMatch(/\/print\/outputs\/o\/preview-slices\/3\/toolpath\?slicer=native$/)
    expect(api.previewToolpathUrl('o', 3, 'bambuddy')).toMatch(/\/print\/outputs\/o\/preview-slices\/3\/toolpath$/)
    expect(api.libraryPreviewToolpathUrl(7, 3, 'native')).toMatch(/\/print\/library\/7\/preview-slices\/3\/toolpath\?slicer=native$/)
    expect(api.libraryPreviewToolpathUrl(7, 3, 'bambuddy')).toMatch(/\/print\/library\/7\/preview-slices\/3\/toolpath$/)
  })
})
