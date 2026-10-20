import { screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { bridge } from '../../agent/bridge'
import { isUserOnly } from '../../agent/dom'
import { useGlobalAgentTools } from '../../agent/global'
import {
  changeFlowTrigger,
  flowTriggerRequests,
  reset as resetFlowTriggers,
} from '../../mocks/features/flowTriggers'
import { renderPage } from '../../test/utils'
import { FlowTriggers } from './Triggers'

// #2484 (spec 2026-10-11 §12) — a flow's triggers: next fire, last fires and their runs,
// pause and resume, and the standing grant attached, approved or declined by the person
// as the trigger was shown (`seen`).

function Agent({ children }: { children: ReactNode }) {
  useGlobalAgentTools()
  return <>{children}</>
}

const render = (wrap = false) => {
  const page = <FlowTriggers name="swap" definitionId="def-swap" schema={null} disabled={false} />
  return renderPage(wrap ? <Agent>{page}</Agent> : page)
}

const row = (name: RegExp) => screen.findByRole('listitem', { name })

type User = ReturnType<typeof render>['user']

/** Opens the render trigger's standing grant for review, as a person does. */
async function reviewRenderGrant(user: User) {
  await user.click(within(await row(/When a render finishes/)).getByRole('button', { name: 'Review…' }))
  return screen.findByRole('dialog', { name: 'Review the standing grant' })
}

/** Approves the render trigger's grant as shown, and waits for the dialog to close. */
async function approveRenderGrant(user: User) {
  const dialog = await reviewRenderGrant(user)
  await user.click(await within(dialog).findByRole('button', { name: 'Approve' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
}

describe('FlowTriggers', () => {
  it('lists each trigger with its next fire, its last fires and its grant', async () => {
    render()
    const hourly = await row(/Every hour/)
    expect(within(hourly).getByText(/^Next /)).toBeInTheDocument()
    expect(within(hourly).getByRole('link', { name: 'run, succeeded' })).toHaveAttribute('href', '/workflows/runs/run-done')
    expect(within(hourly).getByText('(overlap)')).toBeInTheDocument()
    expect(within(hourly).getByTestId('standing-grant-status')).toHaveTextContent('In force')
    const render_ = await row(/When a render finishes \(coaster\)/)
    expect(within(render_).getByTestId('standing-grant-status')).toHaveTextContent('Requested by an assistant')
    expect(within(render_).getByText(/outward calls wait for you/)).toBeInTheDocument()
  })

  it('approves a requested grant as it was shown', async () => {
    const { user } = render()
    await approveRenderGrant(user)
    const sent = flowTriggerRequests().filter((r) => r.path === '/flow-triggers/trig-render/grant/approve')
    expect(sent).toHaveLength(1)
    expect(sent[0]!.body?.['seen']).toBe('seen-trig-render-0')
    await waitFor(async () =>
      expect(within(await row(/When a render finishes/)).getByTestId('standing-grant-status')).toHaveTextContent(
        'In force',
      ),
    )
  })

  it('approves a requested grant whatever the date (#4447)', async () => {
    // Years ahead: a mock grant that expires on a fixed date fails here at once, not
    // only once that date has passed.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2031-01-01T00:00:00Z'))
    try {
      resetFlowTriggers()
      const { user } = render()
      await approveRenderGrant(user)
      expect(flowTriggerRequests().filter((r) => r.path === '/flow-triggers/trig-render/grant/approve')).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('refuses an approval of a trigger changed since it was shown, and shows it again', async () => {
    const { user } = render()
    const dialog = await reviewRenderGrant(user)
    // An assistant widens the trigger between the person's look and their click.
    changeFlowTrigger('trig-render', { event: { kind: 'model.changed' } })
    await user.click(await within(dialog).findByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed since it was shown')
    expect(await row(/When a model changes/)).toBeInTheDocument()
    expect(within(await row(/When a model changes/)).getByTestId('standing-grant-status')).toHaveTextContent('Requested')
  })

  it('pauses and resumes', async () => {
    const { user } = render()
    await user.click(within(await row(/Every hour/)).getByRole('button', { name: 'Pause' }))
    await user.click(await within(await row(/Every hour/)).findByRole('button', { name: 'Resume' }))
    await within(await row(/Every hour/)).findByRole('button', { name: 'Pause' })
    expect(flowTriggerRequests().map((r) => r.path)).toEqual([
      '/flow-triggers/trig-hourly/pause',
      '/flow-triggers/trig-hourly/resume',
    ])
  })

  it('revokes after a confirmation, then attaches a new grant as shown', async () => {
    const { user } = render()
    await user.click(within(await row(/Every hour/)).getByRole('button', { name: 'Revoke' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Revoke its grant?' })
    expect(flowTriggerRequests()).toEqual([])
    await user.click(within(confirm).getByRole('button', { name: 'Revoke' }))
    const attach = await within(await row(/Every hour/)).findByRole('button', { name: 'Attach a grant…' })
    await user.click(attach)
    const dialog = await screen.findByRole('dialog', { name: 'Attach a standing grant' })
    // A standing grant asks no starting session: a triggered run has none.
    expect(within(dialog).queryByLabelText('Ask the starting session first')).not.toBeInTheDocument()
    await user.click(await within(dialog).findByRole('button', { name: 'Attach' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    const sent = flowTriggerRequests().find((r) => r.path === '/flow-triggers/trig-hourly/grant')
    expect(sent?.body?.['seen']).toMatch(/^seen-trig-hourly-/)
    expect(sent?.body?.['spec']).toBeDefined()
  })

  it('makes a schedule trigger pinned to this version', async () => {
    const { user } = render()
    await user.click(await screen.findByRole('button', { name: 'New trigger…' }))
    const dialog = await screen.findByRole('dialog', { name: 'New trigger' })
    await user.clear(within(dialog).getByLabelText('Minutes'))
    await user.type(within(dialog).getByLabelText('Minutes'), '30')
    await user.clear(within(dialog).getByLabelText('Time zone'))
    await user.type(within(dialog).getByLabelText('Time zone'), 'Europe/London')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(flowTriggerRequests()).toEqual([
      {
        path: '/flow-triggers',
        body: {
          definition_id: 'def-swap',
          overlap: 'skip',
          schedule: { interval_s: 1800, time_zone: 'Europe/London', offset_s: 0, catchup_window_s: 300 },
        },
      },
    ])
    expect(await row(/Every 30 minutes, Europe\/London/)).toBeInTheDocument()
  })

  // The browser bridge acts in the person's tab as the browser user: everything but
  // Pause is the person's own.
  it('refuses the person-only controls to the browser bridge', async () => {
    render(true)
    await row(/When a render finishes/)
    await waitFor(() => expect(bridge.liveNames()).toContain('click'))
    for (const name of ['New trigger…', 'Review…', 'Decline', 'Revoke']) {
      const outcome = await bridge.call('click', { role: 'button', name })
      expect(!outcome.ok && outcome.error.code, name).toBe('refused')
    }
    // One Delete per trigger: the bridge cannot tell them apart, so look at each.
    for (const button of screen.getAllByRole('button', { name: 'Delete' })) expect(isUserOnly(button)).toBe(true)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(flowTriggerRequests()).toEqual([])
  })
})
