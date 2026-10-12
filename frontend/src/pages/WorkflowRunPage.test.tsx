import { act, screen, waitFor, within } from '@testing-library/react'
import { http } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { fakeRealtime } from '../lib/realtime.fake'
import {
  flowRequests,
  moveFlowRunOn,
  moveFlowRunPastPreview,
  setFlowRunOffline,
  setFlowRunStarting,
} from '../mocks/features/flows'
import { mockSettings, setMockSettings } from '../mocks/handlers'
import { server } from '../mocks/server'
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

// #2350 — the version a run ran and its script, forgetting a run, and a Reset.

function renderWithExits(id: string) {
  return renderPage(
    <Routes>
      <Route path="/workflows/runs/:id" element={<WorkflowRunPage />} />
      <Route path="/workflows" element={<p>Runs list</p>} />
    </Routes>,
    { route: `/workflows/runs/${id}` },
  )
}

describe('WorkflowRunPage, #2350', () => {
  it('links to the version it ran, says who started it, and shows its script', async () => {
    render('run-print')
    expect(await screen.findByRole('link', { name: 'plates v2' })).toHaveAttribute('href', '/workflows/flows/def-plates-2')
    expect(screen.getByText(/you, in the browser/)).toBeInTheDocument()
    const script = await screen.findByLabelText('Script')
    expect(script).toHaveTextContent('await queue_print')
    expect(script.querySelector('[data-token="keyword"]')).toHaveTextContent('async')
  })

  it('forgets a finished run once confirmed, saying what is lost', async () => {
    const { user } = renderWithExits('run-done')
    await user.click(await screen.findByRole('button', { name: 'Forget run' }))
    const dialog = await screen.findByRole('dialog', { name: 'Forget this run?' })
    expect(dialog).toHaveTextContent('Its steps, your answers and approvals, and its result are deleted')
    expect(dialog).toHaveTextContent('This can’t be undone.'.replace('’', "'"))
    expect(dialog).not.toHaveTextContent('It is stopped first')
    expect(flowRequests()).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Forget run' }))
    expect(await screen.findByText('Runs list')).toBeInTheDocument()
    expect(flowRequests()).toEqual([{ path: '/api/v1/workflow-runs/run-done', key: null, body: {} }])
  })

  it('says a run still going is stopped first', async () => {
    const { user } = renderWithExits('run-ask')
    await user.click(await screen.findByRole('button', { name: 'Stop and forget' }))
    const dialog = await screen.findByRole('dialog', { name: 'Forget this run?' })
    expect(dialog).toHaveTextContent('This run has not finished. It is stopped first')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(flowRequests()).toEqual([])
  })

  it('previews a reset with outward calls flagged, and resets only once confirmed', async () => {
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    const point = await screen.findByRole('combobox', { name: 'Reset point' })
    expect(within(point).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Choose a point…',
      'Event 11: before step 1 (render)',
      'Event 21: before step 2 (queue_print)',
      'Event 31: before step 3 (sleep)',
      'Event 37: after the last step',
    ])
    await user.selectOptions(point, 'Event 11: before step 1 (render)')
    const again = await screen.findByRole('list', { name: 'Calls that run again' })
    const items = within(again).getAllByRole('listitem')
    expect(items.map((i) => i.textContent)).toEqual(['#1 render', '#2 queue_printOutward: sent again', '#3 sleep'])
    expect(items[1]).toHaveAttribute('data-outward', 'true')

    await user.click(screen.getByRole('button', { name: 'Reset to this point…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Reset this run?' })
    expect(within(dialog).getByRole('list', { name: 'Outward calls sent again' })).toHaveTextContent('#2 queue_print')
    expect(flowRequests()).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Reset run' }))
    expect(await screen.findByText('Reset. The run continues from that point.')).toBeInTheDocument()
    expect(flowRequests()).toEqual([
      {
        path: '/api/v1/workflow-runs/run-done/reset',
        key: expect.stringMatching(KEY),
        body: { event_id: 11, as_of_event_id: 100, workflow_run_id: 'wr-run-done' },
      },
    ])
    await waitFor(() => expect(screen.getByTestId('flow-run-status')).toHaveTextContent('Running'))
  })

  it('shows the preview of the point chosen last, though an earlier one answers after it', async () => {
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get('/api/v1/workflow-runs/:id/reset-preview', async ({ request }) => {
        // Held, then answered by the feature's own handler.
        if (new URL(request.url).searchParams.get('event_id') === '11') await held
      }),
    )
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    const point = await screen.findByRole('combobox', { name: 'Reset point' })
    await user.selectOptions(point, 'Event 11: before step 1 (render)')
    await user.selectOptions(point, 'Event 31: before step 3 (sleep)')
    await screen.findByRole('list', { name: 'Calls that run again' })
    release()
    await new Promise((resolve) => setTimeout(resolve, 100))
    const again = screen.getByRole('list', { name: 'Calls that run again' })
    expect(within(again).getAllByRole('listitem').map((i) => i.textContent)).toEqual(['#3 sleep'])
  })

  it('a point past every step runs nothing again', async () => {
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Reset point' }), 'Event 37: after the last step')
    expect(await screen.findByText('No recorded call runs again.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reset to this point…' }))
    expect(await screen.findByRole('dialog', { name: 'Reset this run?' })).toHaveTextContent(
      'No outward call (a print, an arrange) is sent again.',
    )
  })

  it('shows the new preview when outward calls landed since the one seen', async () => {
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    await user.selectOptions(
      await screen.findByRole('combobox', { name: 'Reset point' }),
      'Event 21: before step 2 (queue_print)',
    )
    await user.click(await screen.findByRole('button', { name: 'Reset to this point…' }))
    moveFlowRunPastPreview('run-done')
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Reset run' }))
    expect(await screen.findByRole('status')).toHaveTextContent('The run made more outward calls since that preview')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reset to this point…' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Reset run' }))
    await screen.findByText('Reset. The run continues from that point.')
    expect(flowRequests().map((r) => r.body['as_of_event_id'])).toEqual([100, 110])
  })
})
