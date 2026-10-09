import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { server } from '../mocks/server'
import { ImportDialog } from './ImportDialog'

const URL_ = 'https://raw.githubusercontent.com/someone/models/main/vase.scad'

/** setTimeout held, so the countdown moves only when the test says (as CatalogueFilters). */
function heldClock() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  vi.stubGlobal('jest', { advanceTimersByTime: (ms: number) => vi.advanceTimersByTime(ms) })
  onTestFinished(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })
  return userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
}

/** One second at a time: each tick schedules the next once React has rendered it. */
async function seconds(n: number) {
  for (let i = 0; i < n; i += 1) await act(async () => vi.advanceTimersByTimeAsync(1000))
}

describe('ImportDialog (#1295)', () => {
  it('counts a spent import budget down, then offers the import again without sending it', async () => {
    let posts = 0
    server.use(
      http.post('/api/v1/models/import', () => {
        posts += 1
        return HttpResponse.json(
          {
            title: 'Service Unavailable',
            status: 503,
            detail: '2 imports are already fetching on this replica; try again in 3 s',
            retry_after: 3,
          },
          { status: 503, headers: { 'Retry-After': '3', 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    const user = heldClock()
    render(<ImportDialog open onClose={vi.fn()} onImported={vi.fn()} />)
    await user.type(screen.getByLabelText('URL'), URL_)
    await user.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Try again in 3 s.')
    const again = screen.getByRole('button', { name: 'Try again' })
    expect(again).toBeDisabled()

    await seconds(2)
    expect(screen.getByRole('status')).toHaveTextContent('Try again in 1 s.')
    expect(again).toBeDisabled()

    await seconds(1)
    expect(screen.getByRole('status')).toHaveTextContent('You can try again now.')
    expect(again).toBeEnabled()
    // Never on its own: an import is the user's to send.
    await seconds(10)
    expect(posts).toBe(1)

    await user.click(again)
    await vi.waitFor(() => expect(posts).toBe(2))
  })

  it('shows no countdown for a refusal that asks no wait', async () => {
    server.use(
      http.post('/api/v1/models/import', () =>
        HttpResponse.json(
          { title: 'Unprocessable', status: 422, detail: 'not a .scad file' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const user = userEvent.setup()
    render(<ImportDialog open onClose={vi.fn()} onImported={vi.fn()} />)
    await user.type(screen.getByLabelText('URL'), URL_)
    await user.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('not a .scad file')
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.getByRole('button', { name: 'Import' })).toBeEnabled()
  })
})
