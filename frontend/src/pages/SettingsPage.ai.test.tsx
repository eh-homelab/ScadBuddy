import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'

// #251 — "MCP access tokens" talks to the agent service, so Settings shows it only
// where the AI features are available (`useAiAvailability()`), as the assistant does.

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

  it('hides MCP access tokens, and asks the agent service nothing, when AI is unavailable', async () => {
    const list = vi.spyOn(api, 'listMcpTokens')
    const auth = vi.spyOn(api, 'getMcpAuth')
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByRole('region', { name: 'MCP access tokens' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'MCP authentication' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(list).not.toHaveBeenCalled()
    expect(auth).not.toHaveBeenCalled()
  })

  it('shows MCP access tokens when AI is available', async () => {
    availability.available = true
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByRole('region', { name: 'MCP access tokens' })).toBeInTheDocument()
    expect(await screen.findByRole('list', { name: 'Tokens' })).toBeInTheDocument()
  })

  it('shows MCP authentication beside the tokens, and the token list follows a saved mode', async () => {
    availability.available = true
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const auth = screen.getByRole('region', { name: 'MCP authentication' })
    await user.click(await within(auth).findByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(within(auth).getByRole('button', { name: 'Save' }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: 'Turn authentication off' }),
    )
    const tokens = screen.getByRole('region', { name: 'MCP access tokens' })
    expect(await within(tokens).findByTestId('mcp-auth-note')).toHaveTextContent(
      'MCP authentication is turned off',
    )
  })
})
