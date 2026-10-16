import { act, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ATTENTION_POLL_MS } from '../../agent/attention'
import { setFlowRunChildren } from '../../mocks/features/flows'
import { renderPage } from '../../test/utils'
import { SessionFlowRuns } from './SessionFlowRuns'

// #1057 — the panel's link to the flow runs a session started (plan 2026-10-09 Task F2),
// over the msw runs (src/mocks/features/flows.ts: `run-ask` was started by `sess-1`).

describe('SessionFlowRuns', () => {
  it('links to the runs the session started, and shows nothing for a session that started none', async () => {
    const { unmount } = renderPage(<SessionFlowRuns sessionId="sess-1" />)
    expect(await screen.findByRole('link', { name: 'Flow runs started here (1)' })).toHaveAttribute(
      'href',
      '/workflows?session=sess-1',
    )
    unmount()
    const { container } = renderPage(<SessionFlowRuns sessionId="sess-none" />)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(container).toBeEmptyDOMElement()
  })

  // #2482 — the starter's panel shows each open run's one queue, grouped by run, with
  // where each entry waits and why (spec 2026-10-11 §4.5). `run-escalated` was started
  // by `sess-escalated`: one call with it, one narrowed and with the user, one child's.
  it('lists what each open run started here waits on, where, and why', async () => {
    renderPage(<SessionFlowRuns sessionId="sess-escalated" />)
    const waiting = await screen.findByRole('list', { name: 'Waiting in runs started here' })
    expect(within(waiting).getByRole('link', { name: 'swap' })).toHaveAttribute('href', '/workflows/runs/run-escalated')
    const lines = within(waiting)
      .getAllByRole('listitem')
      .slice(1)
      .map((li) => li.textContent)
    expect(lines).toHaveLength(3)
    expect(lines[0]).toMatch(/^queue_print: with this session until .+; outside the grant: print limit reached$/)
    expect(lines[1]).toBe('arrange: with you; outside the grant: no rule admits it')
    expect(lines[2]).toBe('print_output (in a session it talks to): with you; outside the grant: printer 5 is not in the grant')
  })

  it("re-reads the queues while a child's call is listed, which no run topic announces", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderPage(<SessionFlowRuns sessionId="sess-escalated" />)
      const waiting = await screen.findByRole('list', { name: 'Waiting in runs started here' })
      expect(waiting).toHaveTextContent('print_output (in a session it talks to)')
      setFlowRunChildren('run-escalated', [])
      await act(() => vi.advanceTimersByTimeAsync(ATTENTION_POLL_MS))
      await waitFor(() => expect(screen.getByRole('list', { name: 'Waiting in runs started here' })).not.toHaveTextContent('print_output'))
    } finally {
      vi.useRealTimers()
    }
  })
})
