import { SpanStatusCode, trace } from '@opentelemetry/api'
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api, ApiError } from '../api/client'
import { choicesView, queuedResult } from '../mocks/choices'
import type { SlotChoice } from '../api/types'
import { installTestTracing } from '../test/tracing'
import { DEFAULT_NOZZLES } from './printChoices'
import type { PrintSelection } from './usePrintChoices'
import { useRunPrint } from './useRunPrint'

const OUTPUT = 'a'.repeat(32)
/** Hoisted: a fresh `[]` per render would itself count as a change of plan. */
const PLAN: SlotChoice[] = []

const selection: PrintSelection = {
  nozzles: DEFAULT_NOZZLES,
  tier: 'standard',
  processName: null,
  bedType: 'Textured PEI Plate',
  overrides: {},
  plate: 'all',
}

/**
 * The run's inputs. Every array and object in them is shared, so two calls are the same
 * choices and only an override is a change.
 */
function input(overrides: Partial<PrintSelection> = {}) {
  return {
    source: { kind: 'output' as const, output: { id: OUTPUT, slug: 'name-keychain' } },
    choices: choicesView,
    printerId: 1,
    selection: { ...selection, ...overrides },
    plan: PLAN,
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

  it('stops following the run when the dialog unmounts', async () => {
    let signal: AbortSignal | undefined
    runPrint.mockImplementationOnce((_output, _body, given) => {
      signal = given
      return new Promise(() => undefined)
    })
    const { result, unmount } = renderHook(() => useRunPrint(input()))
    void act(() => void result.current.run())
    await waitFor(() => expect(signal).toBeDefined())
    expect(signal?.aborted).toBe(false)

    unmount()

    expect(signal?.aborted).toBe(true)
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

    // The same choices again are still refused.
    rerender(input())
    expect(result.current.refused).toBe(true)
    expect(result.current.runError).toBe('No process for 0.2 mm.')

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

describe('useRunPrint, traced', () => {
  afterEach(() => vi.restoreAllMocks())

  it('records the run as a print span naming the output, printer and plate, with the run request inside it', async () => {
    const tracing = installTestTracing()
    try {
      let active: string | undefined
      vi.spyOn(api, 'runPrint').mockImplementation(async () => {
        active = trace.getActiveSpan()?.spanContext().spanId
        return queuedResult
      })
      vi.spyOn(api, 'putModelChoices').mockResolvedValue(undefined as never)
      vi.spyOn(api, 'putPrinterBedType').mockResolvedValue(undefined as never)
      const { result } = renderHook(() => useRunPrint(input()))
      await act(() => result.current.run())

      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.name).toBe('print')
      expect(span?.attributes).toEqual({
        'scadbuddy.output_id': OUTPUT,
        'scadbuddy.printer_id': 1,
        'scadbuddy.plate_id': 1,
        'scadbuddy.all_plates': true,
      })
      expect(active).toBe(span?.spanContext().spanId)
    } finally {
      tracing.uninstall()
    }
  })

  it('marks a refused run as an error with its problem type', async () => {
    const tracing = installTestTracing()
    try {
      vi.spyOn(api, 'runPrint').mockRejectedValue(
        new ApiError({ type: 'https://scadbuddy.dev/problems/unresolvable', title: 'No', status: 422, detail: 'secret' }),
      )
      const { result } = renderHook(() => useRunPrint(input()))
      await act(() => result.current.run())
      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.status.code).toBe(SpanStatusCode.ERROR)
      expect(span?.attributes['scadbuddy.failure_class']).toBe('https://scadbuddy.dev/problems/unresolvable')
    } finally {
      tracing.uninstall()
    }
  })
})
