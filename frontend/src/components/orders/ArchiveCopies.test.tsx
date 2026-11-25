import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { ArchiveOffer, AttemptArchives, Fulfilment } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { BROWSER, ITEM_APPROVED, ORDER_ACCEPTED, state } from '../../mocks/features/orders/state'
import { server } from '../../mocks/server'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'
import { ARCHIVE_READ_MS, ARCHIVE_READS } from './ArchiveCopies'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

const ARCHIVES = '/api/v1/orders/:id/items/:item/fulfilments/:f/archives'

/** An attempt whose print settled: what the print path records. */
function printedAttempt(): Fulfilment {
  const attempt: Fulfilment = {
    id: 'f-printed',
    order_item_id: ITEM_APPROVED,
    purpose: 'production',
    attempt: 1,
    quantity: 2,
    outcome: 'succeeded',
    failure_reason: null,
    started_at: '2026-11-01T10:00:00Z',
    finished_at: '2026-11-01T12:00:00Z',
    notes: '',
    accepted_at: null,
    created_by: BROWSER,
    created_at: '2026-11-01T10:00:00Z',
    print_run_id: 'run-printed',
    copies: 2,
  }
  state.fulfilments.push(attempt)
  return attempt
}

function render() {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`, userEventOptions: { advanceTimers: vi.advanceTimersByTime } },
  )
}

/** Every read of the attempt's archives answers `offer`; the returned list counts the reads. */
function answer(attempt: Fulfilment, offer: Partial<ArchiveOffer>): string[] {
  const reads: string[] = []
  server.use(
    http.get(ARCHIVES, ({ params }) => {
      reads.push(String(params.f))
      return HttpResponse.json<AttemptArchives>({
        linked: true,
        attempt,
        archives: [
          {
            archive_id: 8001,
            status: 'completed',
            finish_photos: ['finish_8001.jpg'],
            timelapse: false,
            completed_at: '2026-11-01T12:00:00Z',
            finished_too_large: false,
            timelapse_too_large: false,
            ...offer,
          },
        ],
      })
    }),
  )
  return reads
}

// Spec 2026-11-05 §4.4 (owner's decision §8.2): the row loads when the page opens, shows
// what each archive has attached, and offers a copy only of what is still missing.
describe('ArchiveCopies', () => {
  beforeEach(() => {
    resetMockState()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => vi.useRealTimers())

  it('loads on open with no click and shows what the import attached', async () => {
    printedAttempt()
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    expect(screen.queryByRole('button', { name: 'From Bambuddy…' })).toBeNull()
    await waitFor(() => expect(archives).toHaveTextContent('Archive 8001'))
    // The read started the import and answered at once: nothing is attached yet.
    expect(archives).toHaveTextContent('Importing finish photo')
    // Reading the archives copied what they say onto the attempt.
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Attempts' })).getByText(/9\.5 g/)).toBeInTheDocument())

    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS)
    await waitFor(() => expect(archives).toHaveTextContent('Finish photo attached'))
    expect(archives).toHaveTextContent('Timelapse attached')
    expect(within(archives).queryByRole('button')).toBeNull()
    const imported = state.attachments.filter((a) => a.item_id === ITEM_APPROVED)
    expect(imported.map((a) => [a.archive_media, a.caption])).toEqual([
      ['finished_photo', 'Attempt 1, Bambuddy archive 8001'],
      ['timelapse', 'Attempt 1, Bambuddy archive 8001'],
    ])
  })

  it('shows what is attached after a reload, and reads no more', async () => {
    const attempt = printedAttempt()
    // Two reads before the page opens: the first starts the import, the second sees it done.
    await api.attemptArchives(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)
    await api.attemptArchives(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)
    const read = vi.spyOn(api, 'attemptArchives')
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent('Finish photo attached'))
    expect(archives).toHaveTextContent('Timelapse attached')
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS * 3)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it(`reads again every ${ARCHIVE_READ_MS / 1000} s at most ${ARCHIVE_READS} times, then offers a copy of only what is missing`, async () => {
    const attempt = printedAttempt()
    const reads = answer(attempt, { finished_attachment_id: 'att-photo', timelapse: true })
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent('Importing timelapse'))
    expect(within(archives).queryByRole('button', { name: 'Copy timelapse' })).toBeNull()
    expect(reads).toHaveLength(1)

    for (let n = 1; n <= ARCHIVE_READS; n++) {
      await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS)
      await waitFor(() => expect(reads).toHaveLength(1 + n))
    }
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS * 3)
    expect(reads).toHaveLength(1 + ARCHIVE_READS)

    expect(await within(archives).findByRole('button', { name: 'Copy timelapse' })).toBeEnabled()
    expect(within(archives).queryByRole('button', { name: 'Copy finish photo' })).toBeNull()
    expect(archives).toHaveTextContent('Finish photo attached')
  })

  it.each([
    ['has no completion time', null, 'waiting for Bambuddy to process the timelapse'],
    ['completed under an hour ago', 10 * 60_000, 'waiting for Bambuddy to process the timelapse'],
    ['completed an hour ago or more', 61 * 60_000, 'no timelapse in Bambuddy for this print'],
  ])('says which timelapse state applies when the archive %s', async (_name, ago, text) => {
    const attempt = printedAttempt()
    const completed = ago == null ? null : new Date(Date.now() - ago).toISOString()
    const reads = answer(attempt, { finished_attachment_id: 'att-photo', timelapse: false, completed_at: completed })
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent(text))
    expect(archives).not.toHaveTextContent(/print's page/)
    expect(within(archives).queryByRole('button')).toBeNull()
    // Nothing is offered and missing, so the row reads no more.
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS * 2)
    expect(reads).toHaveLength(1)
  })

  it('says a medium the import refused as too large is attached by hand, offers no copy, and reads no more', async () => {
    const attempt = printedAttempt()
    const reads = answer(attempt, { finished_attachment_id: 'att-photo', timelapse: true, timelapse_too_large: true })
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent('Timelapse is over the upload limit; attach it by hand'))
    expect(archives).toHaveTextContent('Finish photo attached')
    expect(within(archives).queryByRole('button')).toBeNull()
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS * 2)
    expect(reads).toHaveLength(1)
  })

  it(`waits ${ARCHIVE_READ_MS / 1000} s after each answer, so a read slower than that is not dropped`, async () => {
    const attempt = printedAttempt()
    const offer = (attached: boolean): ArchiveOffer => ({
      archive_id: 8001,
      status: 'completed',
      finish_photos: ['finish_8001.jpg'],
      finished_attachment_id: attached ? 'att-photo' : null,
      timelapse: false,
      completed_at: '2026-11-01T12:00:00Z',
      finished_too_large: false,
      timelapse_too_large: false,
    })
    let reads = 0
    server.use(
      http.get(ARCHIVES, async () => {
        const n = ++reads
        // The second read takes longer than the wait between reads and sees the photo
        // attached; a third would answer much later and see nothing new.
        if (n === 2) await delay(ARCHIVE_READ_MS * 1.5)
        if (n > 2) await delay(ARCHIVE_READ_MS * 100)
        return HttpResponse.json<AttemptArchives>({ linked: true, attempt, archives: [offer(n === 2)] })
      }),
    )
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent('Importing finish photo'))
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS)
    await waitFor(() => expect(reads).toBe(2))
    // act flushes React's work after each step, so a re-read due at 20 s starts before 25 s.
    await act(() => vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS))
    await act(() => vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS / 2))
    await waitFor(() => expect(archives).toHaveTextContent('Finish photo attached'))
    expect(reads).toBe(2)
  })

  it('reads again when the timelapse wait ends, and then says the archive has no timelapse', async () => {
    const attempt = printedAttempt()
    const completed = new Date(Date.now() - 50 * 60_000).toISOString()
    const reads = answer(attempt, { finished_attachment_id: 'att-photo', timelapse: false, completed_at: completed })
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent('waiting for Bambuddy to process the timelapse'))
    expect(reads).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    await waitFor(() => expect(reads).toHaveLength(2))
    await waitFor(() => expect(archives).toHaveTextContent('no timelapse in Bambuddy for this print'))
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS * 2)
    expect(reads).toHaveLength(2)
  })

  it('"Read again" clears the error and shows the read until it answers', async () => {
    const attempt = printedAttempt()
    let reads = 0
    server.use(
      http.get(ARCHIVES, async () => {
        reads += 1
        if (reads === 1) return HttpResponse.json({ detail: 'Bambuddy did not answer' }, { status: 502 })
        await delay(ARCHIVE_READ_MS / 2)
        return HttpResponse.json<AttemptArchives>({ linked: true, attempt, archives: [] })
      }),
    )
    const { user } = render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await within(archives).findByRole('alert')
    await user.click(within(archives).getByRole('button', { name: 'Read again' }))
    await waitFor(() => expect(archives).toHaveTextContent("Reading Bambuddy's archives…"))
    expect(within(archives).queryByRole('alert')).toBeNull()
    expect(within(archives).queryByRole('button', { name: 'Read again' })).toBeNull()
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS / 2)
    await waitFor(() => expect(archives).toHaveTextContent("Bambuddy has not linked this print's archive yet."))
    expect(reads).toBe(2)
  })

  it('reads again after a re-read that failed', async () => {
    const attempt = printedAttempt()
    let reads = 0
    server.use(
      http.get(ARCHIVES, () => {
        reads += 1
        if (reads === 2) return HttpResponse.json({ detail: 'Bambuddy did not answer' }, { status: 502 })
        return HttpResponse.json<AttemptArchives>({
          linked: true,
          attempt,
          archives: [
            {
              archive_id: 8001,
              status: 'completed',
              finish_photos: ['finish_8001.jpg'],
              finished_attachment_id: reads > 2 ? 'att-photo' : null,
              timelapse: false,
              completed_at: '2026-11-01T12:00:00Z',
              finished_too_large: false,
              timelapse_too_large: false,
            },
          ],
        })
      }),
    )
    render()
    const archives = await screen.findByLabelText("Attempt 1's Bambuddy archives")
    await waitFor(() => expect(archives).toHaveTextContent('Importing finish photo'))
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS)
    await within(archives).findByRole('alert')
    await vi.advanceTimersByTimeAsync(ARCHIVE_READ_MS)
    await waitFor(() => expect(archives).toHaveTextContent('Finish photo attached'))
    expect(reads).toBe(3)
  })

  it('is not shown for an attempt recorded by hand that names no archive', async () => {
    const attempt = printedAttempt()
    attempt.print_run_id = null
    render()
    await screen.findByRole('list', { name: 'Attempts' })
    expect(screen.queryByLabelText("Attempt 1's Bambuddy archives")).toBeNull()
  })
})
