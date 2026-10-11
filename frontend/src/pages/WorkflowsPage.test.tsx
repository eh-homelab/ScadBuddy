import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { fakeRealtime } from '../lib/realtime.fake'
import { setFlowRuns } from '../mocks/features/flows'
import { renderPage } from '../test/utils'
import { WorkflowsPage } from './WorkflowsPage'

// #1057 — the Workflows list (plan 2026-10-09 Task F1) over the msw flow runs
// (src/mocks/features/flows.ts).

function render(route = '/workflows') {
  return renderPage(
    <Routes>
      <Route path="/workflows" element={<WorkflowsPage />} />
    </Routes>,
    { route },
  )
}

const list = () => screen.findByRole('list', { name: 'Flow runs' })
const row = (id: string) => document.querySelector<HTMLElement>(`[data-run="${id}"]`)!

describe('WorkflowsPage', () => {
  it('lists each run with its flow and version, its status and what it waits on', async () => {
    render()
    await list()
    expect(within(row('run-ask')).getByRole('link', { name: 'swap v1' })).toHaveAttribute('href', '/workflows/runs/run-ask')
    expect(row('run-ask')).toHaveTextContent('Waiting')
    expect(row('run-ask')).toHaveTextContent('Waiting for your answer: Swap to pink?')
    expect(within(row('run-print')).getByRole('link', { name: 'plates v2' })).toBeInTheDocument()
    expect(row('run-print')).toHaveTextContent('Waiting for approval: queue_print')
    expect(row('run-done')).toHaveTextContent('Succeeded')
  })

  it('keeps the runs one assistant session started', async () => {
    render('/workflows?session=sess-1')
    const runs = await list()
    expect(within(runs).getAllByRole('listitem')).toHaveLength(1)
    expect(row('run-ask')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Show all runs' })).toHaveAttribute('href', '/workflows')
  })

  it('reads again when a run changes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const realtime = fakeRealtime()
      render()
      await list()
      setFlowRuns([])
      await realtime.signal('workflow-runs', 'flow_run.changed')
      await waitFor(() => expect(screen.getByText('No flow runs yet.')).toBeInTheDocument())
    } finally {
      vi.useRealTimers()
    }
  })
})
