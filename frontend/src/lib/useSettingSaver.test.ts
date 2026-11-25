import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { LabelOutput } from '../api/filament'
import { server } from '../mocks/server'
import { useSettingSaver } from './useSettingSaver'

/** Every `PUT /settings` body in `puts`; the first answer waits for `answer()`, and fails when `failFirst`. */
function slowFirstPut(failFirst = false) {
  const puts: unknown[] = []
  let answer: () => void = () => {}
  const held = new Promise<void>((resolve) => (answer = resolve))
  server.use(
    http.put('*/api/v1/settings', async ({ request }) => {
      puts.push(await request.json())
      if (puts.length === 1) {
        await held
        if (failFirst) return HttpResponse.json({ detail: 'no' }, { status: 500 })
      }
      return HttpResponse.json({})
    }),
  )
  return { puts, answer: () => answer() }
}

afterEach(() => {
  vi.restoreAllMocks()
})

/**
 * Each `api.putSettings` call the hook makes, as the promise of its answer. `settled(n)` waits until the hook made
 * `n` calls and each answered, then one macrotask, which runs after the hook's code that follows an answer
 * (microtasks only): an assert then sees what the hook did with every answer, however slow the runner.
 */
function watchPuts() {
  const real = api.putSettings.bind(api)
  const answers: Promise<unknown>[] = []
  vi.spyOn(api, 'putSettings').mockImplementation((body) => {
    const answer = real(body)
    answers.push(answer.catch(() => {}))
    return answer
  })
  return async function settled(count: number) {
    await waitFor(() => expect(answers).toHaveLength(count))
    await Promise.all(answers)
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
  }
}

const pdf: LabelOutput = { format: 'pdf', dpi: 300, fields: ['number', 'qr'] }
const png203: LabelOutput = { format: 'png', dpi: 203, fields: ['number', 'qr'] }
const png600: LabelOutput = { format: 'png', dpi: 600, fields: ['number', 'qr'] }

describe('useSettingSaver (R-7157-9, R-7216-16)', () => {
  it('sends one PUT at a time, and then only the newest of the waiting values', async () => {
    const { puts, answer } = slowFirstPut()
    const settled = watchPuts()
    const { result } = renderHook(() => useSettingSaver('filament_label_output'))
    expect(result.current.savedBeforeRead).toBe(false)
    act(() => result.current.save(pdf))
    await waitFor(() => expect(puts).toHaveLength(1))
    act(() => result.current.save(png203))
    act(() => result.current.save(png600))
    expect(result.current.savedBeforeRead).toBe(true)
    expect(puts).toHaveLength(1)
    answer()
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts).toEqual([{ filament_label_output: pdf }, { filament_label_output: png600 }])
    // Once both answers are in and the hook has acted on them, it has made no third call.
    await settled(2)
    expect(api.putSettings).toHaveBeenCalledTimes(2)
  })

  it('does not show the failure of a save that a newer one replaced', async () => {
    const { puts, answer } = slowFirstPut(true)
    const settled = watchPuts()
    const { result } = renderHook(() => useSettingSaver('filament_label_output'))
    act(() => result.current.save(pdf))
    await waitFor(() => expect(puts).toHaveLength(1))
    act(() => result.current.save(png600))
    answer()
    await waitFor(() => expect(puts).toHaveLength(2))
    await settled(2)
    expect(result.current.failed).toBe(false)
  })

  it('shows the failure of the newest save', async () => {
    const { puts, answer } = slowFirstPut(true)
    const { result } = renderHook(() => useSettingSaver('filament_label_output'))
    act(() => result.current.save(pdf))
    await waitFor(() => expect(puts).toHaveLength(1))
    answer()
    await waitFor(() => expect(result.current.failed).toBe(true))
  })
})
