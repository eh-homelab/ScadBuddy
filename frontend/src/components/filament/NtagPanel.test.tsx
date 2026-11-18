import { act, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getSpool } from '../../api/filament'
import { spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { NtagPanel } from './NtagPanel'

afterEach(() => {
  vi.useRealTimers()
  window.localStorage.clear()
})

const conflict = (detail: string, code: string) =>
  HttpResponse.json({ title: 'Conflict', status: 409, detail, code }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } })

async function render(number = 7) {
  const spool = await getSpool(spoolId(number))
  const onBound = vi.fn()
  const view = renderPage(<NtagPanel spool={spool} onBound={onBound} />, { userEventOptions: { advanceTimers: (ms) => void (vi.isFakeTimers() && vi.advanceTimersByTime(ms)) } })
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Reader' })).toHaveTextContent('spoolbuddy-bench'))
  return { ...view, onBound }
}

describe('NtagPanel', () => {
  it('offers only the readers with NFC', async () => {
    const { user } = await render()
    await user.click(screen.getByRole('combobox', { name: 'Reader' }))
    expect(await screen.findAllByRole('option')).toHaveLength(1)
  })

  it('writes, then polls every 2 s until the tag is bound', async () => {
    const polls: number[] = []
    let n = 0
    server.use(
      http.post('*/api/v1/filament/spools/:id/ntag/confirm', () => {
        polls.push(++n)
        return HttpResponse.json(n >= 2 ? { bound: true, tag_uid: '04A1' } : { bound: false })
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user, onBound } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await screen.findByText('Hold a blank tag to the reader')
    expect(polls).toEqual([])
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await waitFor(() => expect(polls).toEqual([1]))
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(await screen.findByText('Bound tag 04A1')).toBeInTheDocument()
    expect(onBound).toHaveBeenCalled()
    await act(() => vi.advanceTimersByTimeAsync(10000))
    expect(polls).toEqual([1, 2])
  })

  it('binds the written tag on the spool once the mock confirms it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await screen.findByText('Hold a blank tag to the reader')
    await act(() => vi.advanceTimersByTimeAsync(2000))
    await act(() => vi.advanceTimersByTimeAsync(2000))
    expect(await screen.findByText(/Bound tag /)).toBeInTheDocument()
    expect((await getSpool(spoolId(7))).tag?.kind).toBe('ntag_opentag')
  })

  it('says not seen yet after 60 s', async () => {
    server.use(http.post('*/api/v1/filament/spools/:id/ntag/confirm', () => HttpResponse.json({ bound: false })))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await screen.findByText('Hold a blank tag to the reader')
    await act(() => vi.advanceTimersByTimeAsync(63000))
    expect(await screen.findByText(/Not seen yet/)).toBeInTheDocument()
  })

  it('cancels the write and stops polling', async () => {
    let cancelled: unknown
    let polls = 0
    server.use(
      http.post('*/api/v1/filament/spools/:id/ntag/cancel', async ({ request }) => {
        cancelled = await request.json()
        return new HttpResponse(null, { status: 204 })
      }),
      http.post('*/api/v1/filament/spools/:id/ntag/confirm', () => {
        polls++
        return HttpResponse.json({ bound: false })
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(cancelled).toEqual({ device_id: 'sb-0001' }))
    await act(() => vi.advanceTimersByTimeAsync(6000))
    expect(polls).toBe(0)
    expect(screen.getByRole('button', { name: 'Write NTAG' })).toBeEnabled()
  })

  it('can cancel after the 60 s check has ended', async () => {
    let cancelled: unknown
    server.use(
      http.post('*/api/v1/filament/spools/:id/ntag/confirm', () => HttpResponse.json({ bound: false })),
      http.post('*/api/v1/filament/spools/:id/ntag/cancel', async ({ request }) => {
        cancelled = await request.json()
        return new HttpResponse(null, { status: 204 })
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await act(() => vi.advanceTimersByTimeAsync(63000))
    await screen.findByText(/Not seen yet/)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(cancelled).toEqual({ device_id: 'sb-0001' }))
  })

  it('keeps the reader fixed while a write is queued, so Cancel reaches that reader', async () => {
    server.use(http.post('*/api/v1/filament/spools/:id/ntag/confirm', () => HttpResponse.json({ bound: false })))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = await render()
    const reader = screen.getByRole('combobox', { name: 'Reader' })
    expect(reader).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await screen.findByText('Hold a blank tag to the reader')
    expect(reader).toBeDisabled()
    await act(() => vi.advanceTimersByTimeAsync(63000))
    await screen.findByText(/Not seen yet/)
    expect(reader).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(reader).toBeEnabled())
  })

  it('drops a queued write when the person leaves the page', async () => {
    let cancelled: unknown
    server.use(
      http.post('*/api/v1/filament/spools/:id/ntag/cancel', async ({ request }) => {
        cancelled = await request.json()
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user, unmount } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    await screen.findByText('Hold a blank tag to the reader')
    unmount()
    await waitFor(() => expect(cancelled).toEqual({ device_id: 'sb-0001' }))
  })

  it('says when the readers could not be read', async () => {
    server.use(http.get('*/api/v1/filament/rfid/status', () => HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'Bambuddy refused' }, { status: 502, headers: { 'Content-Type': 'application/problem+json' } })))
    const spool = await getSpool(spoolId(7))
    renderPage(<NtagPanel spool={spool} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy refused')
  })

  it('lists the warnings of the write', async () => {
    server.use(http.post('*/api/v1/filament/spools/:id/ntag/write', () => HttpResponse.json({ status: 'queued', warnings: ['no colour on the record'] })))
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    expect(await screen.findByText('no colour on the record')).toBeInTheDocument()
  })

  it('asks before it replaces a Bambu tag, then retries with replace', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('*/api/v1/filament/spools/:id/ntag/write', async ({ request }) => {
        const body = (await request.json()) as { replace: boolean }
        bodies.push(body)
        return body.replace ? HttpResponse.json({ status: 'queued', warnings: [] }) : conflict('open override tag', 'override_tag_open')
      }),
    )
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    expect(await screen.findByText('This replaces the Bambu tag the AMS reads. Replace it?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Replace' }))
    await screen.findByText('Hold a blank tag to the reader')
    expect(bodies).toEqual([expect.objectContaining({ replace: false }), expect.objectContaining({ replace: true })])
  })

  it('shows the detail of a spool with no Bambuddy record and starts no polling', async () => {
    let polls = 0
    server.use(
      http.post('*/api/v1/filament/spools/:id/ntag/write', () => conflict('this spool is not linked to a Bambuddy spool', 'no_bambuddy_spool')),
      http.post('*/api/v1/filament/spools/:id/ntag/confirm', () => {
        polls++
        return HttpResponse.json({ bound: false })
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = await render()
    await user.click(screen.getByRole('button', { name: 'Write NTAG' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('this spool is not linked to a Bambuddy spool')
    await act(() => vi.advanceTimersByTimeAsync(6000))
    expect(polls).toBe(0)
  })
})
