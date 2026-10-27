import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { ApprovalsWaitingContext, type PendingApproval } from '../../agent/attention'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ApprovalsWaiting } from './ApprovalsWaiting'

// #4813 — an approval an MCP client prepared has no session, so no chat feed has its card:
// the badge counts it, and this list (in the panel and on Activity) is where it is decided.
const approval: PendingApproval = {
  id: 'approval:a1',
  tool: 'print_output',
  summary: '{"output_id":"o1"}',
  by: 'Claude Code',
  expiresAt: '2026-10-27T10:10:00.000Z',
  inputHash: 'b'.repeat(64),
}

/** The agent's respond route, answering as it does; returns what it was sent. */
function agentResponds() {
  const sent: { id: string; body: unknown }[] = []
  server.use(
    http.post('/api/v1/ai/pending-input/:id', async ({ params, request }) => {
      const body = (await request.json()) as { decision: string }
      sent.push({ id: String(params.id), body })
      return HttpResponse.json({ id: params.id, kind: 'approval', outcome: body.decision === 'approve' ? 'approved' : 'denied' })
    }),
  )
  return sent
}

const shown = (approvals: readonly PendingApproval[], refresh = vi.fn()) => ({
  refresh,
  ...renderPage(
    <ApprovalsWaitingContext.Provider value={{ approvals, refresh }}>
      <ApprovalsWaiting />
    </ApprovalsWaitingContext.Provider>,
  ),
})

describe('ApprovalsWaiting', () => {
  it('shows each with what it does and who asked, and approves it through the respond route', async () => {
    const sent = agentResponds()
    const { user, refresh } = shown([approval])
    const card = within(screen.getByRole('region', { name: 'Needs your approval' }))
    expect(card.getByText('print_output, asked by Claude Code')).toBeInTheDocument()
    expect(card.getByText('{"output_id":"o1"}')).toBeInTheDocument()
    const approve = card.getByRole('button', { name: 'Approve' })
    // Human-only: the browser bridge never presses it.
    expect(approve).toHaveAttribute('data-agent-user-only')
    await user.click(approve)
    expect(await card.findByRole('status')).toHaveTextContent('Approved.')
    // With the call's input hash: the decision holds only for the summary shown.
    expect(sent).toEqual([{ id: 'approval:a1', body: { kind: 'approval', decision: 'approve', input_hash: 'b'.repeat(64) } }])
    // The badge reads again, so the card goes once the agent no longer lists it.
    expect(refresh).toHaveBeenCalled()
  })

  it('denies', async () => {
    const sent = agentResponds()
    const { user } = shown([approval])
    await user.click(screen.getByRole('button', { name: 'Deny' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Denied.')
    expect(sent).toEqual([{ id: 'approval:a1', body: { kind: 'approval', decision: 'deny', input_hash: 'b'.repeat(64) } }])
  })

  it('says why a decision was not taken once the approval no longer waits', async () => {
    server.use(
      http.post('/api/v1/ai/pending-input/:id', () =>
        HttpResponse.json({ detail: 'no longer waiting', reason: 'nobody replied in time' }, { status: 409 }),
      ),
    )
    const { user } = shown([approval])
    await user.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Your decision was not taken: nobody replied in time.')
  })

  it('keeps the buttons, with the error, when the agent could not be reached', async () => {
    server.use(http.post('/api/v1/ai/pending-input/:id', () => HttpResponse.error()))
    const { user } = shown([approval])
    await user.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Your decision was not taken: The assistant could not be reached; try again.')
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
  })

  it('shows nothing when none waits, or outside the shell', () => {
    const { container } = shown([])
    expect(container).toBeEmptyDOMElement()
    const outside = renderPage(<ApprovalsWaiting />)
    expect(outside.container).toBeEmptyDOMElement()
  })
})
