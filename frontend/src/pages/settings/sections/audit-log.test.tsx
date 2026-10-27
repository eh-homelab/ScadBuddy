import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderSettingsSection } from '../testing'
import { Component as AuditLog } from './audit-log'

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
  })

  it('shows no link while the assistant is unavailable, so the section stays empty', async () => {
    availability.available = false
    renderSettingsSection(<AuditLog />, { route: '/settings/audits' })
    await screen.findByRole('region', { name: 'Audit log' })
    expect(document.getElementById('audit-log-body')?.childElementCount).toBe(0)
  })
})
