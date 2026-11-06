import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { ATTENTION_POLL_MS, RunsWaitingContext, type RunWaiting } from '../../agent/attention'
import { setFlowDetails } from '../../mocks/features/flows'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RunsWaiting } from './RunsWaiting'

// #2482 — the badge counts a flow run's own calls waiting for the person; the panel it
// opens lists those runs, read from the badge's own poll, since a browser-started run has
// no session to list them under.
const shown = (runs: readonly RunWaiting[] | null) =>
  renderPage(
    <RunsWaitingContext.Provider value={runs}>
      <RunsWaiting />
    </RunsWaitingContext.Provider>,
  )

describe('RunsWaiting', () => {
  it('lists each run with calls waiting for you, linking to its page', () => {
    shown([
      { id: 'run-1', name: 'swap', waiting: 2 },
      { id: 'run-3', name: 'tidy', waiting: 1 },
    ])
    const list = screen.getByRole('list', { name: 'Flow runs waiting for you' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'swap waits for you on 2 calls',
      'tidy waits for you on a call',
    ])
    expect(within(list).getByRole('link', { name: 'swap' })).toHaveAttribute('href', '/workflows/runs/run-1')
  })

  it("names each run by its flow's display name once the flows are read (#3186)", async () => {
    setFlowDetails('swap', { display_name: 'Nightly swap' })
    shown([{ id: 'run-1', name: 'swap', waiting: 1 }])
    const list = screen.getByRole('list', { name: 'Flow runs waiting for you' })
    expect(await within(list).findByRole('link', { name: 'Nightly swap' })).toHaveAttribute('href', '/workflows/runs/run-1')
  })

  it('keeps the slug when the flows cannot be read', async () => {
    setFlowDetails('swap', { display_name: 'Nightly swap' })
    let asked = 0
    server.use(
      http.get('/api/v1/workflows', () => {
        asked += 1
        return HttpResponse.json({ detail: 'down' }, { status: 500 })
      }),
    )
    shown([{ id: 'run-1', name: 'swap', waiting: 1 }])
    await waitFor(() => expect(asked).toBeGreaterThan(0))
    const list = screen.getByRole('list', { name: 'Flow runs waiting for you' })
    await waitFor(() => expect(within(list).getByRole('link', { name: 'swap' })).toHaveAttribute('href', '/workflows/runs/run-1'))
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['swap waits for you on a call'])
  })

  it('reads the display names again on the badge\'s poll, so a rename shows while it is open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      setFlowDetails('swap', { display_name: 'Nightly swap' })
      shown([{ id: 'run-1', name: 'swap', waiting: 1 }])
      const list = screen.getByRole('list', { name: 'Flow runs waiting for you' })
      expect(await within(list).findByRole('link', { name: 'Nightly swap' })).toBeInTheDocument()
      setFlowDetails('swap', { display_name: 'Renamed swap' })
      await act(() => vi.advanceTimersByTimeAsync(ATTENTION_POLL_MS))
      await waitFor(() => expect(within(list).getByRole('link', { name: 'Renamed swap' })).toBeInTheDocument())
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows nothing when no run waits for you, or outside the shell', () => {
    expect(shown([]).container).toBeEmptyDOMElement()
    expect(shown(null).container).toBeEmptyDOMElement()
  })
})
