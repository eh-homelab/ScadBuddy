import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderSettingsSection } from '../testing'
import { AUDIT_LOGS, Component as AuditLog } from './audit-log'

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

describe('Audit log', () => {
  afterEach(() => {
    availability.available = true
  })

  it('links to the Activity page for background jobs (spec §3.2)', async () => {
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    const section = await screen.findByRole('region', { name: 'Audit log' })
    expect(section).toHaveTextContent('Background jobs are on the Activity page.')
    expect(within(section).getByRole('link', { name: 'Activity page' })).toHaveAttribute('href', '/activity')
    const panel = screen.getByRole('tabpanel', { name: 'AI activity' })
    expect(within(panel).queryByRole('link', { name: 'Activity page' })).toBeNull()
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
    // The AI card keeps its own gate.
    expect(within(section).queryByRole('list', { name: 'AI activity entries' })).toBeNull()
    expect(document.getElementById('ai-audit-heading')).toBeNull()
  })
})
