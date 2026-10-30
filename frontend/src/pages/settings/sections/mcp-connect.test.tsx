import { act, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { McpTokensSection } from '../../../components/McpTokensSection'
import { mcpAuthChanged } from '../../../lib/mcpAuthChanges'
import { mockSettings } from '../../../mocks/handlers'
import { setMcpAuthMode } from '../../../mocks/features/mcpTokens'
import { server } from '../../../mocks/server'
import { renderSettingsSection } from '../testing'
import { Component as McpConnect } from './mcp-connect'
import { Component as McpAccessTokens } from './mcp-access-tokens'

// #781 — the Connect snippet is a section of its own beside Tokens. A token just minted
// still fills it (#1910) through lib/mcpMintedToken.ts, until Done or the card goes.

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

const route = '/settings/mcp'
const snippet = () => screen.getByTestId('mcp-snippet-claude-code').textContent ?? ''

describe('Connect snippet section', () => {
  beforeEach(() => {
    availability.available = true
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...mockSettings(), public_url: 'https://scadbuddy.example' }),
      ),
    )
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('fills the snippet with a token just minted, until Done (#1910)', async () => {
    const { user } = renderSettingsSection(
      <>
        <McpAccessTokens />
        <McpConnect />
      </>,
      { route },
    )
    await screen.findByRole('list', { name: 'Tokens' })
    await waitFor(() =>
      expect(snippet()).toContain("'https://scadbuddy.example/mcp' --header 'Authorization: Bearer <your token>'"),
    )

    await user.type(screen.getByLabelText('Token name'), 'Claude Code')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const token = (await screen.findByTestId('minted-token-value')).textContent!
    expect(snippet()).toContain(`Authorization: Bearer ${token}`)

    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(snippet()).toContain('<your token>')
    expect(document.body.textContent).not.toContain(token)
  })

  it('leaves the header out while MCP authentication is off (#1910)', async () => {
    setMcpAuthMode('disabled')
    renderSettingsSection(<McpConnect />, { route })
    await waitFor(() =>
      expect(snippet()).toBe("claude mcp add --transport http scadbuddy 'https://scadbuddy.example/mcp'"),
    )
  })

  it('follows a saved auth mode', async () => {
    setMcpAuthMode('bearer')
    renderSettingsSection(<McpConnect />, { route })
    await waitFor(() => expect(snippet()).toContain('Authorization'))
    setMcpAuthMode('disabled')
    act(() => mcpAuthChanged())
    await waitFor(() => expect(snippet()).not.toContain('Authorization'))
  })

  it('clears the minted token when the Tokens card unmounts', async () => {
    function Harness() {
      const [shown, setShown] = useState(true)
      return (
        <>
          <button onClick={() => setShown(false)}>Hide tokens</button>
          {shown && <McpTokensSection />}
          <McpConnect />
        </>
      )
    }
    const { user } = renderSettingsSection(<Harness />, { route })
    await screen.findByRole('list', { name: 'Tokens' })
    await user.type(screen.getByLabelText('Token name'), 'Claude Code')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const token = (await screen.findByTestId('minted-token-value')).textContent!
    expect(snippet()).toContain(token)

    await user.click(screen.getByRole('button', { name: 'Hide tokens' }))
    expect(snippet()).toContain('<your token>')
    expect(document.body.textContent).not.toContain(token)
  })

  it('hides itself, and asks the agent nothing, while the assistant is unavailable', async () => {
    availability.available = false
    const auth = vi.spyOn(api, 'getMcpAuth')
    renderSettingsSection(<McpConnect />, { route })
    const body = await waitFor(() => {
      const el = document.getElementById('mcp-connect-body')
      expect(el).not.toBeNull()
      return el!
    })
    expect(body.children).toHaveLength(0)
    expect(auth).not.toHaveBeenCalled()
  })
})
