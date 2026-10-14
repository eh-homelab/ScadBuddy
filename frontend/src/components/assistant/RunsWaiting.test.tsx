import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RunsWaiting } from './RunsWaiting'

// #2482 — the badge counts a flow run's own calls waiting for the person; the panel it
// opens lists those runs, since a browser-started run has no session to list them under.
const entry = (id: string, run: Record<string, unknown>) => ({
  id,
  kind: 'approval',
  session_id: null,
  tool: 'queue_print',
  summary: '',
  input_hash: null,
  prompt: '',
  requested_by: null,
  responders: ['browser'],
  created_at: '2026-10-13T00:00:00Z',
  expires_at: null,
  run,
})

describe('RunsWaiting', () => {
  it('lists each run with calls waiting for you, linking to its page; not one still with its starting session', async () => {
    server.use(
      http.get('/api/v1/ai/pending-input', () =>
        HttpResponse.json({
          entries: [
            entry('a', { id: 'run-1', name: 'swap', hop: null, starter_session: null }),
            entry('b', { id: 'run-1', name: 'swap', hop: 'person', starter_session: null }),
            entry('c', { id: 'run-2', name: 'arrange', hop: 'parent', starter_session: 'sess-1' }),
          ],
        }),
      ),
    )
    renderPage(<RunsWaiting />)
    const list = await screen.findByRole('list', { name: 'Flow runs waiting for you' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['swap waits for you on 2 calls'])
    expect(within(list).getByRole('link', { name: 'swap' })).toHaveAttribute('href', '/workflows/runs/run-1')
  })

  it('shows nothing when no run waits for you', async () => {
    server.use(http.get('/api/v1/ai/pending-input', () => HttpResponse.json({ entries: [] })))
    const { container } = renderPage(<RunsWaiting />)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(container).toBeEmptyDOMElement()
  })
})
