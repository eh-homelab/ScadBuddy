import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
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

  it('revokes only after a confirmation', async () => {
    const { user, onChange } = await render('run-done')
    await user.click(screen.getByRole('button', { name: 'Revoke grant' }))
    expect(flowRequests()).toEqual([])
    const dialog = await screen.findByRole('dialog', { name: 'Revoke this grant?' })
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

  it('says a rule with no bounds lets anything through', async () => {
    const grant = await grantOf('run-done')
    grant.spec.rules = [{ fn: 'arrange' }]
    renderPage(<GrantPanel runId="run-done" grant={grant} onChange={() => {}} />)
    expect(screen.getByText(/any model, any number of calls/)).toHaveClass('text-warn')
  })
})
