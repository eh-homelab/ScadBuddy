import { act, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { fakeRealtime } from '../lib/realtime.fake'
import { flowRequests, moveFlowRunOn, setFlowRunOffline, setFlowRunStarting } from '../mocks/features/flows'
import { mockSettings, setMockSettings } from '../mocks/handlers'
import { renderPage } from '../test/utils'
import { WorkflowRunPage } from './WorkflowRunPage'

// #1057 — one flow run (plan 2026-10-09 Task F1): its steps, and its answers and
// approvals, sent through `command()` with an Idempotency-Key.

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/workflows/runs/:id" element={<WorkflowRunPage />} />
    </Routes>,
    { route: `/workflows/runs/${id}` },
  )
}

const KEY = /^[0-9a-f]{32}$/

describe('WorkflowRunPage', () => {
  it('answers a question with a key, and the run moves on', async () => {
    const { user } = render('run-ask')
    await user.type(await screen.findByLabelText('Your answer'), 'yes')
    await user.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(screen.getByTestId('flow-run-status')).toHaveTextContent('Running'))
    expect(flowRequests()).toEqual([
      { path: '/api/v1/workflow-runs/run-ask/answer', key: expect.stringMatching(KEY), body: { call_id: 'call-2', answer: 'yes' } },
    ])
    expect(screen.queryByLabelText('Your answer')).not.toBeInTheDocument()
  })

  it('approves an outward call', async () => {
    const { user } = render('run-print')
    await user.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(flowRequests()).toHaveLength(1))
    expect(flowRequests()[0]).toEqual({
      path: '/api/v1/workflow-runs/run-print/decide',
      key: expect.stringMatching(KEY),
      body: { call_id: 'call-1', approved: true },
    })
  })

  it('denies with a reason', async () => {
    const { user } = render('run-print')
    await user.type(await screen.findByLabelText('Reason (optional)'), 'not today')
    await user.click(screen.getByRole('button', { name: 'Deny' }))
    await waitFor(() => expect(flowRequests()).toHaveLength(1))
    expect(flowRequests()[0]?.body).toEqual({ call_id: 'call-1', approved: false, reason: 'not today' })
  })

  it('says a stale request is out of date, and reads the run again', async () => {
    const { user } = render('run-print')
    const approve = await screen.findByRole('button', { name: 'Approve' })
    moveFlowRunOn('run-print')
    await user.click(approve)
    expect(await screen.findByRole('alert')).toHaveTextContent('This request is out of date. The run has moved on.')
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument())
  })

  it('says when the live status is unavailable', async () => {
    setFlowRunOffline('run-done')
    render('run-done')
    expect(await screen.findByText('Live status unavailable; showing the last known state')).toBeInTheDocument()
  })

  it('shows a run still starting', async () => {
    setFlowRunStarting('run-new')
    render('run-new')
    expect(await screen.findByTestId('flow-run-status')).toHaveTextContent('Starting…')
    expect(screen.queryByText(/Live status unavailable/)).not.toBeInTheDocument()
  })

  it('lists the steps and links the run in the Temporal UI', async () => {
    setMockSettings({ ...mockSettings(), temporal_ui_url: 'https://temporal.example' })
    render('run-ask')
    const steps = await screen.findByRole('table', { name: 'Steps' })
    expect(steps).toHaveTextContent('render')
    expect(steps).toHaveTextContent('succeeded')
    expect(steps).toHaveTextContent('1m')
    expect(steps).toHaveTextContent('wait_for_human')
    expect(await screen.findByRole('link', { name: 'Temporal UI ↗' })).toHaveAttribute(
      'href',
      'https://temporal.example/namespaces/default/workflows/flow-run-ask',
    )
  })

  it('reads every 5 s while the realtime socket is down', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const realtime = fakeRealtime()
      realtime.setStatus('unavailable')
      render('run-print')
      await screen.findByRole('button', { name: 'Approve' })
      const fetches = vi.spyOn(globalThis, 'fetch')
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      expect(fetches.mock.calls.some(([input]) => String(input instanceof Request ? input.url : input).includes('/workflow-runs/run-print'))).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})
