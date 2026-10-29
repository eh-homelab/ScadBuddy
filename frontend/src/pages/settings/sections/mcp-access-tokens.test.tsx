import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { renderSettingsSection } from '../testing'
import { Component as McpAuthMode } from './mcp-auth-mode'
import { Component as McpAccessTokens } from './mcp-access-tokens'

// #251 — "MCP access tokens" talks to the agent service, so Settings shows it only
// where the AI features are available (`useAiAvailability()`), as the assistant does.

const availability = vi.hoisted(() => ({ available: false }))
vi.mock('../../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

const route = '/settings/mcp'

describe('MCP access: the AI availability gate', () => {
  afterEach(() => {
    availability.available = false
    vi.restoreAllMocks()
  })

  it('hides MCP access tokens, and asks the agent service nothing, when AI is unavailable', async () => {
    const list = vi.spyOn(api, 'listMcpTokens')
    const auth = vi.spyOn(api, 'getMcpAuth')
    renderSettingsSection(
      <>
        <McpAuthMode />
        <McpAccessTokens />
      </>,
      { route },
    )
    expect(await screen.findByRole('region', { name: 'Tokens' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'MCP access tokens' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'MCP authentication' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(list).not.toHaveBeenCalled()
    expect(auth).not.toHaveBeenCalled()
  })

  it('shows MCP access tokens when AI is available', async () => {
    availability.available = true
    renderSettingsSection(
      <>
        <McpAuthMode />
        <McpAccessTokens />
      </>,
      { route },
    )
    expect(await screen.findByRole('region', { name: 'MCP access tokens' })).toBeInTheDocument()
    expect(await screen.findByRole('list', { name: 'Tokens' })).toBeInTheDocument()
  })

  it('shows MCP authentication beside the tokens, and the token list follows a saved mode', async () => {
    availability.available = true
    const { user } = renderSettingsSection(
      <>
        <McpAuthMode />
        <McpAccessTokens />
      </>,
      { route },
    )
    const auth = await screen.findByRole('region', { name: 'MCP authentication' })
    await user.click(await within(auth).findByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(within(auth).getByRole('button', { name: 'Save' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Turn authentication off' }),
    )
    const tokens = screen.getByRole('region', { name: 'MCP access tokens' })
    expect(await within(tokens).findByTestId('mcp-auth-note')).toHaveTextContent(
      'MCP authentication is turned off',
    )
  })
})
