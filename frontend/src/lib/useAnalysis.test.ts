import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api } from '../api/client'
import type { AnalysisReport, AnalysisRequest } from '../api/types'
import { analysisReport } from '../mocks/analyzers'
import * as fixtures from '../mocks/fixtures'
import { fakeRealtime } from './realtime.fake'
import { ANALYSIS_DEBOUNCE_MS, useAnalysis } from './useAnalysis'

const OUTPUT = fixtures.outputs[0]!

type Size = NonNullable<AnalysisRequest['choices']>['nozzles'][number]['size']

function request(size: Size): AnalysisRequest {
  return {
    printer_id: 1,
    plate_id: 1,
    choices: {
      nozzles: [{ size, flow: 'standard' }],
      tier: 'standard',
      bed_type: 'Textured PEI Plate',
    },
  }
}

function report(headline: string): AnalysisReport {
  const built = analysisReport(OUTPUT, request('0.4'))
  return { ...built, summary: { ...built.summary, headline } }
}

async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('useAnalysis', () => {
  let run: MockInstance<typeof api.runAnalyzers>
  let realtime: ReturnType<typeof fakeRealtime>

  beforeEach(() => {
    vi.useFakeTimers()
    run = vi.spyOn(api, 'runAnalyzers')
    realtime = fakeRealtime({ confirm: false })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('judges the output against the request, in advanced detail', async () => {
    run.mockResolvedValue(report('1 suggestion'))
    const { result } = renderHook(() => useAnalysis(OUTPUT.id, request('0.4')))
    await settle()
    expect(run).toHaveBeenCalledTimes(1)
    expect(run).toHaveBeenCalledWith({
      target: { output_id: OUTPUT.id },
      request: request('0.4'),
      detail: 'advanced',
    })
    expect(result.current.report?.summary.headline).toBe('1 suggestion')
    expect(result.current.checking).toBe(false)
  })

  it('runs nothing until the dialog has a request', async () => {
    const { result } = renderHook(() => useAnalysis(OUTPUT.id, null))
    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(run).not.toHaveBeenCalled()
    expect(result.current.report).toBeNull()
    expect(realtime.following()).toEqual([])
  })

  it('waits for the choices to settle, and keeps the last report while it re-runs', async () => {
    run.mockResolvedValue(report('first'))
    const { result, rerender } = renderHook(({ size }) => useAnalysis(OUTPUT.id, request(size)), {
      initialProps: { size: '0.4' as Size },
    })
    await settle()
    run.mockResolvedValue(report('second'))

    rerender({ size: '0.6' })
    rerender({ size: '0.8' })
    await settle(ANALYSIS_DEBOUNCE_MS / 2)
    expect(run).toHaveBeenCalledTimes(1)
    expect(result.current.checking).toBe(true)
    expect(result.current.report?.summary.headline).toBe('first')

    await settle(ANALYSIS_DEBOUNCE_MS)
    expect(run).toHaveBeenCalledTimes(2)
    expect(run.mock.calls[1]![0].request).toEqual(request('0.8'))
    expect(result.current.report?.summary.headline).toBe('second')
    expect(result.current.checking).toBe(false)
  })

  it('reads again when a decision is recorded anywhere', async () => {
    run.mockResolvedValue(report('before'))
    const { result } = renderHook(() => useAnalysis(OUTPUT.id, request('0.4')))
    await settle()
    expect(realtime.following()).toEqual(['analyzers'])

    run.mockResolvedValue(report('after'))
    await realtime.signal('analyzers', 'analyzer.decision')
    await settle()
    expect(run).toHaveBeenCalledTimes(2)
    expect(result.current.report?.summary.headline).toBe('after')
  })

  it('reports a run that failed rather than an old report', async () => {
    run.mockResolvedValue(report('ok'))
    const { result } = renderHook(() => useAnalysis(OUTPUT.id, request('0.4')))
    await settle()
    run.mockRejectedValue(new Error('down'))
    act(() => result.current.reload())
    await settle()
    expect(result.current.error?.message).toBe('down')
    expect(result.current.report).toBeNull()
  })
})
