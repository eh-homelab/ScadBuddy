import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderSettingsSection } from '../testing'
import { AUDIT_LOGS } from '../../../components/audits/logs'
import { Component as AuditLog } from './audit-log'

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

// The arrow-key test needs a second log; the module's list gets one only while it runs.
const extraLog = vi.hoisted(() => ({ on: false }))
vi.mock('../../../components/audits/logs', async (importOriginal) => {
  const original = await importOriginal<{ AUDIT_LOGS: readonly unknown[] }>()
  return {
    get AUDIT_LOGS() {
      return extraLog.on
        ? [...original.AUDIT_LOGS, { value: 'other', label: 'Other log', Component: () => <p>other log body</p> }]
        : original.AUDIT_LOGS
    },
  }
})

describe('Audit log', () => {
  afterEach(() => {
    availability.available = true
    extraLog.on = false
  })

  it('links to the Activity page for background jobs (spec §3.2)', async () => {
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    const section = await screen.findByRole('region', { name: 'Audit log' })
    expect(section).toHaveTextContent('Background jobs are on the Activity page.')
    expect(within(section).getByRole('link', { name: 'Activity page' })).toHaveAttribute('href', '/activity')
    const panel = screen.getByRole('tabpanel', { name: 'AI activity' })
    expect(within(panel).queryByRole('link', { name: 'Activity page' })).toBeNull()
  })

  it('names the log once: the panel has the tab name, the card no region of its own', async () => {
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    const panel = await screen.findByRole('tabpanel', { name: 'AI activity' })
    await within(panel).findByRole('list', { name: 'AI activity entries' })
    expect(within(panel).queryByRole('region')).toBeNull()
  })

  it('moves between tabs with the arrow keys', async () => {
    extraLog.on = true
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    const first = await screen.findByRole('tab', { name: 'AI activity' })
    const user = userEvent.setup()
    await user.click(first)
    await user.keyboard('{ArrowRight}')
    const second = screen.getByRole('tab', { name: 'Other log' })
    await waitFor(() => expect(second).toHaveAttribute('aria-selected', 'true'))
    expect(second).toHaveFocus()
    expect(screen.getByRole('tabpanel', { name: 'Other log' })).toHaveTextContent('other log body')
  })

  it('shows the logs as tabs, even with one (#781)', async () => {
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    const tabs = await screen.findByRole('tablist', { name: 'Audit logs' })
    const names = within(tabs)
      .getAllByRole('tab')
      .map((t) => t.textContent)
    expect(names).toEqual(['AI activity'])
    expect(within(tabs).getByRole('tab', { name: 'AI activity' })).toHaveAttribute('aria-selected', 'true')
    const panel = screen.getByRole('tabpanel', { name: 'AI activity' })
    expect(await within(panel).findByRole('list', { name: 'AI activity entries' })).toBeInTheDocument()
  })

  it('builds one tab per entry of AUDIT_LOGS', () => {
    expect(AUDIT_LOGS.map((l) => l.value)).toEqual(['ai-activity'])
  })

  it('shows the Activity link while the assistant is unavailable', async () => {
    availability.available = false
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    const section = await screen.findByRole('region', { name: 'Audit log' })
    expect(within(section).getByRole('link', { name: 'Activity page' })).toHaveAttribute('href', '/activity')
    expect(within(section).queryByRole('tablist')).toBeNull()
    // The AI card keeps its own gate.
    expect(within(section).queryByRole('list', { name: 'AI activity entries' })).toBeNull()
    expect(document.getElementById('ai-audit-heading')).toBeNull()
  })
})
