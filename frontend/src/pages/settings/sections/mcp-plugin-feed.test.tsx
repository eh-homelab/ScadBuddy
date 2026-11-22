import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setPluginFeed } from '../../../mocks/features/pluginFeeds'
import { renderSettingsSection } from '../testing'
import { Component as McpAccessTokens } from './mcp-access-tokens'
import { Component as PluginFeed } from './mcp-plugin-feed'

// #6933 — Settings → MCP access' plugin feed: what the feed lists, and the setup text that
// registers it in Claude Code with a token.

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

const route = '/settings/mcp'
const URL = 'https://scadbuddy.example/api/v1/ai/plugin-feeds/default/marketplace.json'
const text = (id: string) => screen.getByTestId(id).textContent ?? ''

describe('Plugin feed section', () => {
  beforeEach(() => {
    availability.available = true
  })

  it('lists each plugin with its pin, and why one is not listed', async () => {
    renderSettingsSection(<PluginFeed />, { route })
    const list = await screen.findByRole('list', { name: 'Plugins in the feed' })
    const rows = within(list).getAllByRole('listitem').map((li) => li.textContent)
    expect(rows).toEqual(['greeterVersion 3', 'print-helpersCommit 4b825dc', 'labelsNot listed until its pin is approved'])
  })

  it('gives a settings entry with the bearer header, then the install command', async () => {
    renderSettingsSection(<PluginFeed />, { route })
    await screen.findByTestId('plugin-feed-settings')
    expect(JSON.parse(text('plugin-feed-settings'))).toEqual({
      extraKnownMarketplaces: {
        scadbuddy: { source: { source: 'url', url: URL, headers: { Authorization: 'Bearer <your token>' } } },
      },
    })
    expect(text('plugin-feed-install')).toBe('/plugin install greeter@scadbuddy')
    expect(screen.queryByTestId('plugin-feed-add')).toBeNull()
  })

  it('fills in a token just minted', async () => {
    const { user } = renderSettingsSection(
      <>
        <McpAccessTokens />
        <PluginFeed />
      </>,
      { route },
    )
    await screen.findByRole('list', { name: 'Tokens' })
    await user.type(screen.getByLabelText('Token name'), 'Claude Code')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const token = (await screen.findByTestId('minted-token-value')).textContent!
    await waitFor(() => expect(text('plugin-feed-settings')).toContain(`Bearer ${token}`))
  })

  it('gives the add command alone while MCP authentication is off', async () => {
    setPluginFeed({ authMode: 'disabled' })
    renderSettingsSection(<PluginFeed />, { route })
    await waitFor(() => expect(text('plugin-feed-add')).toBe(`/plugin marketplace add ${URL}`))
    expect(screen.queryByTestId('plugin-feed-settings')).toBeNull()
  })

  it('says when the feed is empty, or has no address', async () => {
    setPluginFeed({ packages: [], marketplaceUrl: null })
    renderSettingsSection(<PluginFeed />, { route })
    expect(await screen.findByText('No plugins are in the feed yet.')).toBeInTheDocument()
    expect(screen.getByText(/The feed has no address/)).toBeInTheDocument()
    expect(screen.queryByTestId('plugin-feed-install')).toBeNull()
  })

  it('asks the agent nothing while the assistant is unavailable', async () => {
    availability.available = false
    renderSettingsSection(<PluginFeed />, { route })
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByTestId('plugin-feed')).toBeNull()
  })
})
