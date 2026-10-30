import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'

// #1921 — the banner over every area while MCP takes calls without a token. The gate on
// the sections themselves (#251) is tested in settings/sections/mcp-access-tokens.test.tsx.

const availability = vi.hoisted(() => ({ available: false }))
vi.mock('../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue(
      'https://bambuddy.internal.nullreference.io',
    ),
  )
}

describe('SettingsPage and the AI availability gate', () => {
  afterEach(() => {
    availability.available = false
    vi.restoreAllMocks()
  })

  it('banners the top of the page while the MCP auth mode is off, until it is back on (#1921)', async () => {
    availability.available = true
    const { user } = renderPage(<SettingsPage />, { route: '/settings/mcp' })
    await seeded()
    expect(screen.queryByTestId('mcp-auth-banner')).toBeNull()
    const auth = await screen.findByRole('region', { name: 'MCP authentication' })
    await user.click(await within(auth).findByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(within(auth).getByRole('button', { name: 'Save' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Turn authentication off' }),
    )
    const banner = await screen.findByTestId('mcp-auth-banner')
    expect(banner).toHaveTextContent('MCP authentication is off')
    // Above every section, not inside one.
    expect(banner.compareDocumentPosition(auth) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    await user.click(within(auth).getByRole('radio', { name: /Require an access token/ }))
    await user.click(within(auth).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByTestId('mcp-auth-banner')).toBeNull())
  })
})
