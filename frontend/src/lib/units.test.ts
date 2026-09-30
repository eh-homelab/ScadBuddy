import { act, renderHook, waitFor } from '@testing-library/react'
import { delay, HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { settings } from '../mocks/fixtures'
import { length, loadDisplayUnit, plateSize, setDisplayUnit, useDisplayUnit } from './units'

describe('length', () => {
  it('keeps one decimal in millimetres and converts to two in inches', () => {
    expect(length(95.74, 'mm')).toBe('95.7')
    expect(length(25.4, 'in')).toBe('1.00')
    expect(length(6.8, 'in')).toBe('0.27')
  })
})

describe('plateSize', () => {
  it('keeps the table’s whole millimetres and converts inches', () => {
    expect(plateSize([256, 256], 'mm')).toBe('256 × 256 mm')
    expect(plateSize([330, 320], 'in')).toBe('12.99 × 12.60 in')
  })
})

describe('useDisplayUnit', () => {
  it('starts in millimetres and follows a change', () => {
    const { result } = renderHook(() => useDisplayUnit())
    expect(result.current).toBe('mm')
    act(() => setDisplayUnit('in'))
    expect(result.current).toBe('in')
  })

  it('loads the stored unit from the settings', async () => {
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json({ ...settings, display_unit: 'in' })),
    )
    const { result } = renderHook(() => useDisplayUnit())
    loadDisplayUnit()
    await waitFor(() => expect(result.current).toBe('in'))
  })

  it('keeps a unit set while the stored one is still loading (a Settings save wins)', async () => {
    let answered = false
    server.use(
      http.get('/api/v1/settings', async () => {
        await delay(50)
        answered = true
        return HttpResponse.json({ ...settings, display_unit: 'mm' })
      }),
    )
    const { result } = renderHook(() => useDisplayUnit())
    loadDisplayUnit()
    act(() => setDisplayUnit('in'))
    await waitFor(() => expect(answered).toBe(true))
    await act(() => delay(0))
    expect(result.current).toBe('in')
  })

  it('stays in millimetres when the settings cannot be read', async () => {
    let calls = 0
    server.use(
      http.get('/api/v1/settings', () => {
        calls += 1
        return HttpResponse.json({ title: 'down' }, { status: 500 })
      }),
    )
    const { result } = renderHook(() => useDisplayUnit())
    loadDisplayUnit()
    await waitFor(() => expect(calls).toBe(1))
    expect(result.current).toBe('mm')
  })
})
