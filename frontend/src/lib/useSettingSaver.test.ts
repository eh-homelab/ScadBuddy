import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
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

const pdf: LabelOutput = { format: 'pdf', dpi: 300, fields: ['number', 'qr'] }
const png203: LabelOutput = { format: 'png', dpi: 203, fields: ['number', 'qr'] }
const png600: LabelOutput = { format: 'png', dpi: 600, fields: ['number', 'qr'] }

describe('useSettingSaver (R-7157-9, R-7216-16)', () => {
  it('sends one PUT at a time, and then only the newest of the waiting values', async () => {
    const { puts, answer } = slowFirstPut()
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
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toHaveLength(2)
  })

  it('does not show the failure of a save that a newer one replaced', async () => {
    const { puts, answer } = slowFirstPut(true)
    const { result } = renderHook(() => useSettingSaver('filament_label_output'))
    act(() => result.current.save(pdf))
    await waitFor(() => expect(puts).toHaveLength(1))
    act(() => result.current.save(png600))
    answer()
    await waitFor(() => expect(puts).toHaveLength(2))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(result.current.error).toBeNull()
  })

  it('shows the failure of the newest save', async () => {
    const { puts, answer } = slowFirstPut(true)
    const { result } = renderHook(() => useSettingSaver('filament_label_output'))
    act(() => result.current.save(pdf))
    await waitFor(() => expect(puts).toHaveLength(1))
    answer()
    await waitFor(() => expect(result.current.error).toBe('The choice could not be saved. It holds on this page only.'))
  })
})
