import { act, screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { fakeRealtime } from '../lib/realtime.fake'
import {
  flowRequests,
  moveFlowRunOn,
  moveFlowRunPastPreview,
  setFlowRunImages,
  setFlowRunOffline,
  setFlowRunStarting,
  childEntries,
  setFlowRunChildren,
} from '../mocks/features/flows'
import { mockSettings, setMockSettings } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { choose } from '../test/select'
import { WorkflowRunPage } from './WorkflowRunPage'
import { ATTENTION_POLL_MS } from '../agent/attention'
import { STALE_ENTRY_MESSAGE } from '../lib/flows'

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
  it('shows the inputs a run started with (#2483)', async () => {
    render('run-coasters')
    const inputs = await screen.findByRole('region', { name: 'Inputs' })
    expect(within(inputs).getAllByRole('term').map((t) => t.textContent)).toEqual([
      'slug',
      'copies',
      'colour',
      'dry_run',
      'plates',
    ])
    expect(within(inputs).getAllByRole('definition').map((d) => d.textContent)).toEqual([
      '"coaster"',
      '2',
      '"red"',
      'false',
      '[1]',
    ])
  })

  it('says a run was started by a trigger, as whoever made it (#2484)', async () => {
    const view = await api.getFlowRun('run-done')
    const fired = new Date(Date.now() - 5 * 60_000).toISOString()
    server.use(
      http.get('/api/v1/workflow-runs/:id', () =>
        HttpResponse.json({ ...view, trigger: { trigger_id: 't1', kind: 'schedule', fire_key: 'k', fired_at: fired } }),
      ),
    )
    render('run-done')
    expect(await screen.findByText(/through a schedule, fired/)).toBeInTheDocument()
  })

  it('shows no inputs for a run of a flow with none', async () => {
    render('run-done')
    expect(await screen.findByRole('region', { name: 'Result' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Inputs' })).toBeNull()
  })

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

  it('sends no blank answer, and shows the answer in flight (#2379)', async () => {
    const { user } = render('run-ask')
    const box = await screen.findByLabelText('Your answer')
    const answer = screen.getByRole('button', { name: 'Answer' })
    expect(answer).toBeDisabled()
    await user.type(box, '   ')
    expect(answer).toBeDisabled()
    await user.type(box, '{Enter}')
    let release!: () => void
    const landed = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.post('*/api/v1/workflow-runs/run-ask/answer', async () => {
        await landed
        return HttpResponse.json({ detail: 'gone' }, { status: 409 })
      }),
    )
    await user.clear(box)
    await user.type(box, 'yes')
    await user.click(answer)
    expect(await screen.findByRole('button', { name: 'Sending…' })).toBeDisabled()
    expect(flowRequests()).toEqual([])
    release()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Sending…' })).toBeNull())
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

  // #2482 — a call outside the run's grant: where it waits, why, and its way up.
  it('says a call waits at the starting session first, why it is outside the grant, and that you may decide now', async () => {
    // The fixture's hop runs to 09:11 on 2026-10-10.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-10T09:05:00Z'))
    try {
      render('run-escalated')
      const card = await screen.findByRole('region', { name: 'Waiting for approval: queue_print' })
      expect(card).toHaveTextContent('With the session that started the run until')
      expect(card).toHaveTextContent('Outside the grant: print limit reached')
      expect(within(card).getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  // #3350: not left saying so until the next read moves the call on.
  it("drops the starting session's notice once its hop's time is up", async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'], shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-10-10T09:10:50Z'))
    try {
      render('run-escalated')
      const card = await screen.findByRole('region', { name: 'Waiting for approval: queue_print' })
      expect(card).toHaveTextContent('With the session that started the run until')
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000)
      })
      expect(card).not.toHaveTextContent('With the session that started the run until')
      expect(card).toHaveTextContent('Outside the grant: print limit reached')
    } finally {
      vi.useRealTimers()
    }
  })

  it("shows a parent's proposal beside the call it narrows, and approves exactly what was shown", async () => {
    const { user } = render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: arrange' })
    const proposal = await within(card).findByRole('table', { name: 'Proposed change' })
    expect(within(proposal).getAllByRole('row').map((r) => r.textContent)).toEqual([
      'FieldAs calledAs proposed',
      'request.objects[0].count32',
      'request.objects[1]{"output_id":"b"}(removed)',
    ])
    expect(card).toHaveTextContent('Narrowed by the starting session')
    // A person's to see and to approve: the assistant can neither read nor click it.
    expect(proposal.closest('[data-agent-user-only]')).not.toBeNull()
    const approve = within(card).getByRole('button', { name: 'Approve as proposed' })
    expect(approve.closest('[data-agent-user-only]')).not.toBeNull()
    await user.click(approve)
    await waitFor(() => expect(flowRequests()).toHaveLength(1))
    expect(flowRequests()[0]?.body).toEqual({ call_id: 'call-narrowed', approved: true, proposal: true, proposal_hash: 'f'.repeat(64) })
  })

  // #3350: a proposal that changes nothing says so, not an empty table.
  it('says a proposal that changes nothing changes nothing', async () => {
    const same = { request: { objects: [{ output_id: 'a', count: 3 }], goal: 'fewest_plates' } }
    server.use(
      http.get('/api/v1/workflow-runs/:id/proposals/:callId', () =>
        HttpResponse.json({
          call_id: 'call-narrowed',
          fn: 'arrange',
          tool: null,
          original: same,
          proposed: same,
          proposal_hash: 'f'.repeat(64),
          proposed_by: 'session:sess-escalated',
        }),
      ),
    )
    render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: arrange' })
    expect(await within(card).findByText('The proposal changes nothing.')).toBeInTheDocument()
    expect(within(card).queryByRole('table', { name: 'Proposed change' })).toBeNull()
  })

  it('sends the reason typed with an approval as proposed, as Approve and Deny do', async () => {
    const { user } = render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: arrange' })
    await within(card).findByRole('table', { name: 'Proposed change' })
    await user.type(within(card).getByLabelText('Reason (optional)'), 'smaller is fine')
    await user.click(within(card).getByRole('button', { name: 'Approve as proposed' }))
    await waitFor(() => expect(flowRequests()).toHaveLength(1))
    expect(flowRequests()[0]?.body).toEqual({
      call_id: 'call-narrowed',
      approved: true,
      reason: 'smaller is fine',
      proposal: true,
      proposal_hash: 'f'.repeat(64),
    })
  })

  it("names each step's hop by where it was, whoever acted, its underscores spaced", async () => {
    render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: arrange' })
    const steps = within(within(card).getByRole('list', { name: 'Its way here' })).getAllByRole('listitem')
    expect(steps.map((s) => s.textContent)).toEqual([
      expect.stringMatching(/^parked by .* \(at the starting session\)$/),
      expect.stringMatching(/^narrow by .* \(at the starting session\)$/),
      expect.stringMatching(/^pass up by .* \(at the person\)$/),
    ])
  })

  // #2482 — the run's one queue: a child session's call is in it too (spec 2026-10-11 §4.5).
  it("shows a child session's call with its step, where it waits, why, and the session's own words", async () => {
    render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: print_output' })
    expect(card).toHaveTextContent('print box on printer 5')
    expect(card).toHaveTextContent('From a session the run talks to, at step 3 (ask_session)')
    expect(card).toHaveTextContent('Outside the grant: printer 5 is not in the grant')
    expect(within(card).getByRole('list', { name: 'Its way here' })).toHaveTextContent('pass up by the starting session')
    const words = within(card).getByRole('figure', { name: 'What the session wrote before the call' })
    expect(words).toHaveTextContent("These are the model's words, not ScadBuddy's")
    expect(words).toHaveTextContent('Printer 5 is idle, so I will print the box there.')
  })

  it("decides a child session's call through the agent, and keeps that decision for the person", async () => {
    const sent: { id: string; body: unknown }[] = []
    server.use(
      http.post('/api/v1/ai/pending-input/:id', async ({ params, request }) => {
        sent.push({ id: String(params['id']), body: await request.json() })
        return HttpResponse.json({ id: params['id'], kind: 'approval', outcome: 'denied' })
      }),
    )
    const { user } = render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: print_output' })
    const deny = within(card).getByRole('button', { name: 'Deny' })
    expect(deny.closest('[data-agent-user-only]')).not.toBeNull()
    await user.click(deny)
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toEqual({ id: 'durable:sess-child:wr-child:toolu_1', body: { kind: 'approval', decision: 'deny' } })
  })

  it("says a child's call already decided is out of date, not the agent's raw refusal", async () => {
    server.use(
      http.post('/api/v1/ai/pending-input/:id', () =>
        HttpResponse.json({ detail: 'durable:sess-child is no longer waiting', reason: 'it was already approved' }, { status: 409 }),
      ),
    )
    const { user } = render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: print_output' })
    await user.click(within(card).getByRole('button', { name: 'Deny' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(STALE_ENTRY_MESSAGE)
  })

  it("re-reads the queue while a child's call shows, which no run topic announces", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      render('run-escalated')
      await screen.findByRole('region', { name: 'Waiting for approval: print_output' })
      setFlowRunChildren('run-escalated', [])
      await act(() => vi.advanceTimersByTimeAsync(ATTENTION_POLL_MS))
      await waitFor(() => expect(screen.queryByRole('region', { name: 'Waiting for approval: print_output' })).toBeNull())
    } finally {
      vi.useRealTimers()
    }
  })

  it("offers no decision on a child's call past its timer", async () => {
    const [entry] = childEntries('run-escalated')
    setFlowRunChildren('run-escalated', [{ ...entry!, expiring: true }])
    render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: print_output' })
    expect(card).toHaveTextContent('Its time is up: ScadBuddy is closing it.')
    expect(within(card).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(within(card).queryByRole('button', { name: 'Deny' })).toBeNull()
  })

  it("names the step of the run's own call", async () => {
    // #3493: the step comes from the agent's queue, a read apart from the run's, so the
    // card can render first. Answered late here, as a slow runner does.
    server.use(
      http.get('/api/v1/ai/flow-runs/:id/queue', async () => {
        await delay(50)
        return undefined
      }),
    )
    render('run-escalated')
    const card = await screen.findByRole('region', { name: 'Waiting for approval: queue_print' })
    await waitFor(() => expect(card).toHaveTextContent('Step 1 (queue_print)'))
  })

  it('keeps the approval of every outward call for the person', async () => {
    render('run-print')
    const approve = await screen.findByRole('button', { name: 'Approve' })
    expect(approve.closest('[data-agent-user-only]')).not.toBeNull()
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

  // #2477: a run's images, as thumbnails from the blob route, never inline data.
  it("shows the run's images as thumbnails from their blob route", async () => {
    const name = `${'a'.repeat(64)}.png`
    setFlowRunImages('run-done', [{ name, mediaType: 'image/png', bytes: 2048 }])
    render('run-done')
    const images = await screen.findByRole('region', { name: 'Images' })
    const img = images.querySelector('img')
    expect(img).toHaveAttribute('src', `/api/v1/workflow-runs/run-done/images/${name}`)
    expect(img?.getAttribute('src')).not.toMatch(/^data:/)
    expect(screen.getByRole('link', { name: /Image 1/ })).toHaveAttribute('href', `/api/v1/workflow-runs/run-done/images/${name}`)
  })

  it('shows no images section for a run with none', async () => {
    render('run-done')
    await screen.findByTestId('flow-run-status')
    expect(screen.queryByRole('region', { name: 'Images' })).not.toBeInTheDocument()
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
    const dialog = await screen.findByRole('alertdialog', { name: 'Forget this run?' })
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
    const dialog = await screen.findByRole('alertdialog', { name: 'Forget this run?' })
    expect(dialog).toHaveTextContent('This run has not finished. It is stopped first')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(flowRequests()).toEqual([])
  })

  it('previews a reset with outward calls flagged, and resets only once confirmed', async () => {
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    const point = await screen.findByRole('combobox', { name: 'Reset point' })
    expect(point).toHaveTextContent('Choose a point…')
    await user.click(point)
    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual([
      'Event 11: before step 1 (render)',
      'Event 21: before step 2 (queue_print)',
      'Event 31: before step 3 (sleep)',
      'Event 37: after the last step',
    ])
    await user.click(screen.getByRole('option', { name: 'Event 11: before step 1 (render)' }))
    const again = await screen.findByRole('list', { name: 'Calls that run again' })
    const items = within(again).getAllByRole('listitem')
    expect(items.map((i) => i.textContent)).toEqual(['#1 render', '#2 queue_printOutward: sent again', '#3 sleep'])
    expect(items[1]).toHaveAttribute('data-outward', 'true')

    await user.click(screen.getByRole('button', { name: 'Reset to this point…' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Reset this run?' })
    expect(within(dialog).getByRole('list', { name: 'Outward calls sent again' })).toHaveTextContent('#2 queue_print')
    expect(flowRequests()).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Reset run' }))
    // The run's reload re-renders the panel, so the first node with the note may be replaced.
    await waitFor(() => expect(screen.getByText('Reset. The run continues from that point.')).toBeInTheDocument())
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
    await choose(user, point, 'Event 11: before step 1 (render)')
    await choose(user, point, 'Event 31: before step 3 (sleep)')
    await screen.findByRole('list', { name: 'Calls that run again' })
    release()
    await new Promise((resolve) => setTimeout(resolve, 100))
    const again = screen.getByRole('list', { name: 'Calls that run again' })
    expect(within(again).getAllByRole('listitem').map((i) => i.textContent)).toEqual(['#3 sleep'])
  })

  it("shows the run's grant and decides a requested one (#2481)", async () => {
    const { user } = render('run-ask')
    const grant = await screen.findByRole('region', { name: 'Grant' })
    expect(within(grant).getByTestId('grant-status')).toHaveTextContent('Requested')
    await user.click(within(grant).getByRole('button', { name: 'Decline grant' }))
    await waitFor(() => expect(within(grant).getByTestId('grant-status')).toHaveTextContent('Declined'))
  })

  it('shows no grant panel for a run without one', async () => {
    render('run-print')
    expect(await screen.findByTestId('flow-run-status')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Grant' })).not.toBeInTheDocument()
  })

  it('a point past every step runs nothing again', async () => {
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Reset point' }), 'Event 37: after the last step')
    expect(await screen.findByText('No recorded call runs again.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reset to this point…' }))
    expect(await screen.findByRole('alertdialog', { name: 'Reset this run?' })).toHaveTextContent(
      'No outward call (a print, an arrange) is sent again.',
    )
  })

  it('shows the new preview when outward calls landed since the one seen', async () => {
    const { user } = render('run-done')
    await user.click(await screen.findByRole('button', { name: 'Preview a reset' }))
    await choose(
      user,
      await screen.findByRole('combobox', { name: 'Reset point' }),
      'Event 21: before step 2 (queue_print)',
    )
    await user.click(await screen.findByRole('button', { name: 'Reset to this point…' }))
    moveFlowRunPastPreview('run-done')
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Reset run' }))
    expect(await screen.findByRole('status')).toHaveTextContent('The run made more outward calls since that preview')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Reset to this point…' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Reset run' }))
    await screen.findByText('Reset. The run continues from that point.')
    expect(flowRequests().map((r) => r.body['as_of_event_id'])).toEqual([100, 110])
  })
})
