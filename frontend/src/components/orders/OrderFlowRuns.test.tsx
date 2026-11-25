import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { FlowRun } from '../../api/types'
import { TASK_NOT_OPEN } from '../../lib/flows'
import { fakeRealtime } from '../../lib/realtime.fake'
import { flowRequests, setFlowRuns } from '../../mocks/features/flows'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ORDER_RUNS_LIMIT, OrderFlowRuns } from './OrderFlowRuns'

// #4252 (orders spec §7.4) — the order's page shows the runs it is for, and each run's
// parked calls with the decide controls the run's own page has.

async function runsNamed(...ids: string[]): Promise<FlowRun[]> {
  const all = (await (await fetch('/api/v1/workflow-runs')).json()) as FlowRun[]
  return ids.map((id) => all.find((r) => r.id === id)!)
}

describe('OrderFlowRuns', () => {
  it("lists the order's runs only, and approves a parked call from the order's page", async () => {
    const [print, done] = await runsNamed('run-print', 'run-done')
    setFlowRuns([{ ...print!, order_id: 'ord-1' }, { ...done!, order_id: 'ord-2' }])
    const { user } = renderPage(<OrderFlowRuns orderId="ord-1" />)
    const section = await screen.findByRole('region', { name: 'Flow runs' })
    expect(within(section).getAllByRole('listitem')).toHaveLength(1)
    expect(within(section).getByRole('link', { name: 'plates v2' })).toHaveAttribute('href', '/workflows/runs/run-print')
    await user.click(await within(section).findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(flowRequests()).toHaveLength(1))
    expect(flowRequests()[0]).toMatchObject({
      path: '/api/v1/workflow-runs/run-print/decide',
      body: { call_id: 'call-1', approved: true },
    })
  })

  it('asks a closed prompt again once the run is at another execution (#7364)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const realtime = fakeRealtime()
      server.use(
        http.post('/api/v1/tasks/:id/answer', () =>
          HttpResponse.json(
            { type: TASK_NOT_OPEN, title: 'Conflict', status: 409, detail: 'no', state: 'open' },
            { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
          ),
        ),
      )
      const [prompt] = await runsNamed('run-prompt')
      setFlowRuns([{ ...prompt!, order_id: 'ord-1' }])
      const { user } = renderPage(<OrderFlowRuns orderId="ord-1" />)
      await user.type(await screen.findByLabelText(/^Email/), 'jo@example.com')
      await user.click(screen.getByRole('button', { name: 'Answer' }))
      expect(await screen.findByText('This question was answered elsewhere.')).toBeInTheDocument()
      setFlowRuns([{ ...prompt!, order_id: 'ord-1', workflow_run_id: 'wr-reset' }])
      await realtime.signal('workflow-run:run-prompt')
      expect(await screen.findByLabelText(/^Email/, {}, { timeout: 3000 })).toHaveValue('')
    } finally {
      vi.useRealTimers()
    }
  })

  it('says when older runs are not shown (review of #5668)', async () => {
    const [done] = await runsNamed('run-done')
    setFlowRuns(
      Array.from({ length: ORDER_RUNS_LIMIT }, (_, i) => ({ ...done!, id: `run-${i}`, order_id: 'ord-1' })),
    )
    renderPage(<OrderFlowRuns orderId="ord-1" />)
    expect(await screen.findByText(/older ones are on the/)).toBeInTheDocument()
  })

  it('shows nothing for an order no run is for', async () => {
    setFlowRuns(await runsNamed('run-done'))
    const { container } = renderPage(<OrderFlowRuns orderId="ord-1" />)
    await waitFor(() => expect(container.querySelector('[role="status"], .animate-pulse')).toBeNull())
    expect(screen.queryByRole('region', { name: 'Flow runs' })).toBeNull()
  })
})
