import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Link } from 'react-router'
import { api } from '../../api/client'
import { reviewPath } from '../../lib/needsYou'
import { server } from '../../mocks/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { respondRequests, setPendingAnswers, setPendingApprovals } from '../../mocks/features/pendingInput'
import { setFlowRunGrant } from '../../mocks/features/flows'
import { changeFlowTrigger } from '../../mocks/features/flowTriggers'
import { resetMockState } from '../../mocks/handlers'
import { NeedsYouHarness } from '../../test/needsYou'
import { renderPage } from '../../test/utils'
import { NeedsYouSection } from './NeedsYou'

const items = () => screen.queryAllByTestId('needs-you-item')
const item = (label: string) => {
  const found = items().find((el) => within(el).queryByRole('heading', { name: label }))
  if (!found) throw new Error(`no "${label}" item`)
  return found
}

describe('NeedsYouSection (#6399)', () => {
  beforeEach(() => {
    resetMockState()
  })

  it('lists every kind that waits on the person, each with its source and how long it has waited', async () => {
    setPendingApprovals(1)
    setPendingAnswers(1, 1)
    renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
      </NeedsYouHarness>,
    )
    const section = await screen.findByRole('region', { name: /Needs you/ })
    await waitFor(() => expect(items()).toHaveLength(5))
    expect(within(section).getByRole('heading', { name: 'Needs you, 5' })).toBeInTheDocument()
    // An MCP client's prepared action: no session, decided here.
    const prepared = item('Prepared action')
    expect(prepared).toHaveTextContent('send_to_bambuddy, asked by You')
    expect(prepared).toHaveTextContent(/asked .* ago/)
    expect(within(prepared).getByRole('button', { name: 'Approve' })).toHaveAttribute('data-agent-user-only')
    expect(item('Question')).toHaveTextContent('Which colour?')
    expect(item('Assistant: lost its ScadBuddy tab')).toHaveTextContent('The ScadBuddy tab closed; reopen it?')
    // The flows' requests the badge never counted.
    const runGrant = item('Grant request')
    expect(runGrant).toHaveTextContent('Flow run swap, asked by')
    expect(item('Standing grant request')).toHaveTextContent('A trigger of swap')
  })

  it("lists the assistant's requests while the grant requests cannot be read, as the shell does", async () => {
    server.use(http.get('/api/v1/flow-grant-requests', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    setPendingApprovals(1)
    renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
      </NeedsYouHarness>,
    )
    await waitFor(() => expect(items()).toHaveLength(1))
    expect(item('Prepared action')).toBeInTheDocument()
  })

  it("opens the review a link names: the bell's Review for a standing grant", async () => {
    const { triggers } = await api.flowGrantRequests()
    const grant = triggers[0]!.grant!
    // The key carries when it was asked for, as the agent's push does (an edit asks anew).
    const key = `trigger-grant:${grant.id}:${Math.floor(Date.parse(grant.requested_at) / 1000)}`
    renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
      </NeedsYouHarness>,
      { route: reviewPath(key) },
    )
    const standing = await waitFor(() => item('Standing grant request'))
    expect(within(standing).getByRole('button', { name: 'Hide trigger' })).toHaveAttribute('aria-expanded', 'true')
    expect(within(item('Grant request')).getByRole('button', { name: 'Review grant…' })).toHaveAttribute('aria-expanded', 'false')
  })

  it("opens a shown row's review when the link changes under it: the bell's Review on /activity", async () => {
    const { triggers } = await api.flowGrantRequests()
    const grant = triggers[0]!.grant!
    const key = `trigger-grant:${grant.id}:${Math.floor(Date.parse(grant.requested_at) / 1000)}`
    const { user } = renderPage(
      <NeedsYouHarness>
        <Link to={reviewPath(key)}>Review from the bell</Link>
        <NeedsYouSection />
      </NeedsYouHarness>,
      { route: '/activity' },
    )
    const standing = await waitFor(() => item('Standing grant request'))
    expect(within(standing).getByRole('button', { name: 'Review grant…' })).toHaveAttribute('aria-expanded', 'false')
    await user.click(screen.getByRole('link', { name: 'Review from the bell' }))
    expect(within(item('Standing grant request')).getByRole('button', { name: 'Hide trigger' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('says how many grant requests wait past the ones listed', async () => {
    const listed = await api.flowGrantRequests()
    server.use(http.get('/api/v1/flow-grant-requests', () => HttpResponse.json({ ...listed, runs_total: listed.runs.length + 2, triggers_total: listed.triggers.length + 1 })))
    renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
      </NeedsYouHarness>,
    )
    expect(await screen.findByText('and 3 more grant requests')).toBeInTheDocument()
  })

  it('approves through the respond route, and the answered request leaves', async () => {
    setPendingApprovals(1)
    const { user } = renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
      </NeedsYouHarness>,
    )
    await waitFor(() => expect(items()).toHaveLength(3))
    await user.click(within(item('Prepared action')).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(items()).toHaveLength(2))
    expect(respondRequests()).toEqual([
      { id: 'approval:00000000-0000-4000-8000-000000000000', body: { kind: 'approval', decision: 'approve', input_hash: '0'.repeat(64) } },
    ])
  })

  it("declines a run's grant through the flow route, and reviews it in full first", async () => {
    const { user } = renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
      </NeedsYouHarness>,
    )
    await waitFor(() => expect(items()).toHaveLength(2))
    const runGrant = item('Grant request')
    await user.click(within(runGrant).getByRole('button', { name: 'Review grant…' }))
    const panel = await within(runGrant).findByRole('region', { name: 'Grant' })
    expect(within(panel).getByRole('table', { name: 'Grant rules' })).toBeInTheDocument()
    await user.click(within(runGrant).getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(items()).toHaveLength(1))
  })

  it('opens the session of a question in the panel', async () => {
    setPendingAnswers(1)
    setFlowRunGrant('run-ask', null)
    changeFlowTrigger('trig-render', { grant: null })
    const openSession = vi.fn()
    const { user } = renderPage(
      <NeedsYouHarness openSession={openSession}>
        <NeedsYouSection />
      </NeedsYouHarness>,
    )
    await waitFor(() => expect(items()).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: 'Answer in the assistant' }))
    expect(openSession).toHaveBeenCalledWith('00000000-0000-4000-8000-000000002000')
  })

  it('shows nothing while nothing waits', async () => {
    setFlowRunGrant('run-ask', null)
    changeFlowTrigger('trig-render', { grant: null })
    renderPage(
      <NeedsYouHarness>
        <NeedsYouSection />
        <p>loaded</p>
      </NeedsYouHarness>,
    )
    await screen.findByText('loaded')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByRole('region', { name: /Needs you/ })).not.toBeInTheDocument()
  })
})
