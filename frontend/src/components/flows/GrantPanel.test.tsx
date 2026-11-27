import { screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { bridge } from '../../agent/bridge'
import { useGlobalAgentTools } from '../../agent/global'
import { api } from '../../api/client'
import type { FlowGrantView } from '../../api/types'
import { flowRequests } from '../../mocks/features/flows'
import { renderPage } from '../../test/utils'
import { GrantPanel } from './GrantPanel'

// #2481 (spec 2026-10-11 §3.2, §3.5) — a run's grant on its page: status, each rule's
// use against its maxima, the calls it let through, Revoke (confirmed), and for a
// requested grant Approve (editor pre-filled, the possibly narrowed spec sent) and Decline.

async function grantOf(runId: string): Promise<FlowGrantView> {
  const view = await api.getFlowRun(runId)
  return view.grant!
}

function Agent({ children }: { children: ReactNode }) {
  useGlobalAgentTools()
  return <>{children}</>
}

async function render(runId: string) {
  const onChange = vi.fn()
  const grant = await grantOf(runId)
  const view = renderPage(<GrantPanel runId={runId} grant={grant} onChange={onChange} />)
  return { ...view, onChange }
}

describe('GrantPanel', () => {
  it('shows an approved grant: status, used against max, and its uses', async () => {
    await render('run-done')
    const panel = screen.getByRole('region', { name: 'Grant' })
    expect(within(panel).getByTestId('grant-status')).toHaveTextContent('In force')
    const rules = within(panel).getByRole('table', { name: 'Grant rules' })
    expect(within(rules).getByText('queue_print')).toBeInTheDocument()
    expect(within(rules).getByText('1 / 4 prints')).toBeInTheDocument()
    expect(within(rules).getByText('12.5 / 200 g')).toBeInTheDocument()
    expect(within(rules).getByText('0 / 2 calls')).toBeInTheDocument()
    const uses = within(panel).getByRole('table', { name: 'Grant uses' })
    expect(within(uses).getAllByRole('row')).toHaveLength(2)
    expect(within(uses).getByText('queue_print')).toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: 'Approve grant…' })).not.toBeInTheDocument()
  })

  it("says who besides the person its grant lets answer the run's questions (#7471)", async () => {
    const grant = await grantOf('run-done')
    renderPage(
      <GrantPanel
        runId="run-done"
        grant={{ ...grant, spec: { ...grant.spec, answers: { who: ['sessions', 'token:70ce0000-0000-4000-8000-00000000000c'] } } }}
        onChange={vi.fn()}
      />,
    )
    expect(
      screen.getByText(/Besides you, any of your assistant sessions and MCP token 70ce0000 may answer its questions/),
    ).toBeInTheDocument()
  })

  it.each([
    ['requested', { status: 'requested', in_force: false }, /^If you approve it, besides you, any of your assistant sessions may answer/],
    ['in force', { status: 'approved', in_force: true }, /^Besides you, any of your assistant sessions may answer/],
    [
      'expired',
      { status: 'approved', in_force: false, expires_at: '2026-01-01T00:00:00Z' },
      /^Expired: any of your assistant sessions may no longer answer/,
    ],
    [
      'revoked',
      { status: 'approved', in_force: false, revoked_at: '2026-11-27T00:00:00Z', revoked_by: 'browser' },
      /^Revoked: any of your assistant sessions may no longer answer/,
    ],
  ] as const)('says who may answer only as a %s grant allows it (#8045)', async (_, state, text) => {
    const grant = await grantOf('run-done')
    renderPage(
      <GrantPanel runId="run-done" grant={{ ...grant, ...state, spec: { ...grant.spec, answers: { who: ['sessions'] } } }} onChange={vi.fn()} />,
    )
    expect(screen.getByText(text)).toBeInTheDocument()
  })

  it('says a grant that lost force with a grant above it is no longer in force, not expired (#8188)', async () => {
    const grant = await grantOf('run-done')
    renderPage(
      <GrantPanel
        runId="run-done"
        grant={{
          ...grant,
          status: 'approved',
          in_force: false,
          revoked_at: null,
          expires_at: '2999-01-01T00:00:00Z',
          spec: { ...grant.spec, answers: { who: ['sessions'] } },
        }}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByTestId('grant-status')).toHaveTextContent('No longer in force')
    expect(screen.getByText(/^No longer in force: any of your assistant sessions may no longer answer/)).toBeInTheDocument()
  })

  it('says nothing of answerers for a declined grant (#8045)', async () => {
    const grant = await grantOf('run-done')
    renderPage(
      <GrantPanel
        runId="run-done"
        grant={{ ...grant, status: 'declined', in_force: false, spec: { ...grant.spec, answers: { who: ['sessions'] } } }}
        onChange={vi.fn()}
      />,
    )
    expect(screen.queryByText(/answer its questions/)).not.toBeInTheDocument()
  })

  it('revokes only after a confirmation', async () => {
    const { user, onChange } = await render('run-done')
    await user.click(screen.getByRole('button', { name: 'Revoke grant' }))
    expect(flowRequests()).toEqual([])
    const dialog = await screen.findByRole('alertdialog', { name: 'Revoke this grant?' })
    await user.click(within(dialog).getByRole('button', { name: 'Revoke grant' }))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(flowRequests().map((r) => r.path)).toEqual(['/api/v1/workflow-runs/run-done/grant/revoke'])
    expect((await grantOf('run-done')).revoked_by).toBe('browser')
  })

  it('approves a requested grant with the narrowed spec', async () => {
    const { user, onChange } = await render('run-ask')
    expect(screen.getByTestId('grant-status')).toHaveTextContent('Requested')
    expect(screen.queryByRole('button', { name: 'Revoke grant' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Approve grant…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Approve this grant' })
    const print = await within(dialog).findByRole('group', { name: 'Rule queue_print' })
    expect(within(print).getByLabelText('Max prints')).toHaveValue('12')
    await user.clear(within(print).getByLabelText('Max prints'))
    await user.type(within(print).getByLabelText('Max prints'), '3')
    await user.click(within(dialog).getByRole('button', { name: 'Approve grant' }))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    const sent = flowRequests().filter((r) => r.path === '/api/v1/workflow-runs/run-ask/grant/approve')
    expect(sent).toHaveLength(1)
    const spec = sent[0]!.body['spec'] as { rules: unknown[] }
    expect(spec.rules).toEqual([{ fn: 'queue_print', printers: [1], models: ['name-keychain'], max_prints: 3, max_calls: 12 }])
    expect((await grantOf('run-ask')).status).toBe('approved')
  })

  it('declines a requested grant', async () => {
    const { user, onChange } = await render('run-ask')
    await user.click(screen.getByRole('button', { name: 'Decline grant' }))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(flowRequests().map((r) => r.path)).toEqual(['/api/v1/workflow-runs/run-ask/grant/decline'])
    expect((await grantOf('run-ask')).status).toBe('declined')
  })

  // The browser bridge acts in the person's tab, as the browser user: every grant
  // decision is user-only, the openers included.
  it('refuses every grant control to the browser bridge', async () => {
    for (const [runId, names] of [
      ['run-ask', ['Approve grant…', 'Decline grant']],
      ['run-done', ['Revoke grant']],
    ] as const) {
      const grant = await grantOf(runId)
      const onChange = vi.fn()
      const { unmount } = renderPage(
        <Agent>
          <GrantPanel runId={runId} grant={grant} onChange={onChange} />
        </Agent>,
      )
      await waitFor(() => expect(bridge.liveNames()).toContain('click'))
      for (const name of names) {
        const outcome = await bridge.call('click', { role: 'button', name })
        expect(!outcome.ok && outcome.error.code).toBe('refused')
      }
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(onChange).not.toHaveBeenCalled()
      unmount()
    }
    expect(flowRequests()).toEqual([])
  })

  it('says a rule with no bounds lets anything through', async () => {
    const grant = await grantOf('run-done')
    grant.spec.rules = [{ fn: 'arrange' }]
    renderPage(<GrantPanel runId="run-done" grant={grant} onChange={() => {}} />)
    expect(screen.getByText(/any model, any number of calls/)).toHaveClass('text-destructive')
  })
})
