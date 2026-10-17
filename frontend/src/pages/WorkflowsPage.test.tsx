import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { fakeRealtime } from '../lib/realtime.fake'
import { flowRequests, setFlowDefinitions, setFlowRuns, setFlowState } from '../mocks/features/flows'
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
const flowRow = (name: string) => document.querySelector<HTMLElement>(`[data-flow="${name}"]`)

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

  it('lists every flow on the Flows tab, with its latest version and who registered it (#2350)', async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/workflows" element={<WorkflowsPage />} />
        <Route path="/workflows/flows" element={<WorkflowsPage tab="flows" />} />
      </Routes>,
      { route: '/workflows' },
    )
    await list()
    await user.click(screen.getByRole('link', { name: 'Flows' }))
    const flows = await screen.findByRole('list', { name: 'Flows' })
    const plates = flows.querySelector<HTMLElement>('[data-flow="plates"]')!
    expect(within(plates).getByRole('link', { name: 'plates' })).toHaveAttribute('href', '/workflows/flows/def-plates-2')
    expect(plates).toHaveTextContent('latest v2')
    // `GET /workflows` is each name's newest version: one row per flow.
    expect(flows.querySelectorAll('[data-flow]')).toHaveLength(3)
    expect(plates).toHaveTextContent('Registered by you, in the browser')
    const swap = flows.querySelector<HTMLElement>('[data-flow="swap"]')!
    expect(swap).toHaveTextContent('latest v1')
    expect(swap).toHaveTextContent('Registered by an assistant (session sess-1)')
    expect(screen.getByRole('link', { name: 'Flows' })).toHaveAttribute('aria-current', 'page')
    // #2484: its triggers under it, each with its last run and pause or resume.
    const triggers = await within(swap).findByRole('list', { name: 'Triggers' })
    const hourly = within(triggers).getByRole('listitem', { name: /Every hour/ })
    expect(within(hourly).getByRole('link', { name: 'run, succeeded' })).toHaveAttribute('href', '/workflows/runs/run-done')
    expect(within(hourly).getByRole('button', { name: 'Pause' })).toBeInTheDocument()
    expect(plates.querySelector('[aria-label="Triggers"]')).toBeNull()
  })

  it('archives a flow from the Flows tab, after saying what happens to its runs, and shows it again (#2485)', async () => {
    const { user } = renderPage(<WorkflowsPage tab="flows" />)
    const flows = await screen.findByRole('list', { name: 'Flows' })
    const swap = () => flowRow('swap')
    await user.click(within(swap()!).getByRole('button', { name: 'Archive' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Archive swap?' })
    expect(dialog).toHaveTextContent('Runs in progress keep running')
    // The agent may confirm an archive, as before #3653; a delete stays the user's own.
    expect(within(dialog).getByRole('button', { name: 'Archive' })).not.toHaveAttribute('data-agent-user-only')
    await user.click(within(dialog).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(swap()).toBeNull())
    expect(within(flows).getAllByRole('listitem')).toHaveLength(2)

    await user.click(screen.getByRole('switch', { name: 'Show archived' }))
    await waitFor(() => expect(swap()).not.toBeNull())
    expect(swap()).toHaveTextContent('Archived by you, in the browser')
    await user.click(within(swap()!).getByRole('button', { name: 'Unarchive' }))
    await waitFor(() => expect(swap()).not.toHaveTextContent('Archived'))
    expect(flowRequests().map((r) => r.path)).toEqual([
      '/api/v1/workflows/names/swap/archive',
      '/api/v1/workflows/names/swap/unarchive',
    ])
  })

  it('deletes a flow from the Flows tab and restores it as its next version (#2485)', async () => {
    const { user } = renderPage(<WorkflowsPage tab="flows" />)
    const flows = await screen.findByRole('list', { name: 'Flows' })
    const plates = () => flowRow('plates')
    expect(flows).toBeInTheDocument()
    await user.click(within(plates()!).getByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete plates?' })
    expect(dialog).toHaveTextContent('Runs in progress keep running on the version they started with')
    expect(dialog).toHaveTextContent('Finished runs still show the script they ran')
    await user.click(within(dialog).getByRole('button', { name: 'Delete flow' }))
    await waitFor(() => expect(plates()).toBeNull())

    await user.click(screen.getByRole('switch', { name: 'Show archived' }))
    await waitFor(() => expect(plates()).toHaveTextContent('Deleted by you, in the browser'))
    expect(within(plates()!).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    await user.click(within(plates()!).getByRole('button', { name: 'Restore v2' }))
    await waitFor(() => expect(plates()).toHaveTextContent('latest v3'))
    expect(plates()).not.toHaveTextContent('Deleted')
  })

  it('lists archived and deleted flows only when asked (#2485)', async () => {
    setFlowState({ name: 'swap', archived_at: '2026-10-12T00:00:00Z', archived_by: { kind: 'agent', session: 's9' } })
    const { user } = renderPage(<WorkflowsPage tab="flows" />)
    const flows = await screen.findByRole('list', { name: 'Flows' })
    expect(flows.querySelector('[data-flow="swap"]')).toBeNull()
    await user.click(screen.getByRole('switch', { name: 'Show archived' }))
    await waitFor(() => expect(flowRow('swap')).toHaveTextContent('Archived by an assistant (session s9)'))
  })

  it("starts a flow from the Flows tab, asking for its inputs first (#2483)", async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/workflows/flows" element={<WorkflowsPage tab="flows" />} />
        <Route path="/workflows/runs/:id" element={<p>Run page</p>} />
      </Routes>,
      { route: '/workflows/flows' },
    )
    await screen.findByRole('list', { name: 'Flows' })
    await user.click(within(flowRow('coasters')!).getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start coasters' })
    await user.type(within(dialog).getByRole('textbox', { name: /Template/ }), 'lid')
    await user.click(within(dialog).getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(flowRequests().map((r) => [r.path, r.body])).toEqual([
      [
        '/api/v1/workflows/def-coasters/runs',
        { inputs: { slug: 'lid', copies: 2, colour: 'red', dry_run: false, plates: [1] } },
      ],
    ])
  })

  it('starts a flow with no inputs from the Flows tab at once, and offers no start for an archived one', async () => {
    setFlowState({ name: 'plates', archived_at: '2026-10-12T00:00:00Z', archived_by: { kind: 'browser' } })
    const { user } = renderPage(
      <Routes>
        <Route path="/workflows/flows" element={<WorkflowsPage tab="flows" />} />
        <Route path="/workflows/runs/:id" element={<p>Run page</p>} />
      </Routes>,
      { route: '/workflows/flows' },
    )
    await screen.findByRole('list', { name: 'Flows' })
    await user.click(screen.getByRole('switch', { name: 'Show archived' }))
    await waitFor(() => expect(flowRow('plates')).not.toBeNull())
    expect(within(flowRow('plates')!).queryByRole('button', { name: 'Start run' })).toBeNull()
    await user.click(within(flowRow('swap')!).getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(flowRequests().map((r) => [r.path, r.body])).toEqual([['/api/v1/workflows/def-swap/runs', {}]])
  })

  it('says when no flow is registered', async () => {
    setFlowDefinitions([])
    renderPage(<WorkflowsPage tab="flows" />)
    expect(await screen.findByText(/No flows registered yet/)).toBeInTheDocument()
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
