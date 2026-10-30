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

  it('shows no snippet until the auth mode is read, so no header flashes for disabled', async () => {
    setMcpAuthMode('disabled')
    let release: () => void = () => {}
    const real = api.getMcpAuth.bind(api)
    vi.spyOn(api, 'getMcpAuth').mockImplementation(
      () => new Promise((resolve) => (release = () => resolve(real()))),
    )
    renderSettingsSection(<McpConnect />, { route })
    await waitFor(() => expect(api.getMcpAuth).toHaveBeenCalled())
    expect(screen.queryByTestId('mcp-snippet-claude-code')).toBeNull()
    release()
    await waitFor(() => expect(snippet()).not.toContain('Authorization'))
  })

  it('keeps the snippet on screen while a saved auth mode is read again (#781)', async () => {
    setMcpAuthMode('bearer')
    renderSettingsSection(<McpConnect />, { route })
    await waitFor(() => expect(snippet()).toContain('Authorization'))
    let release: () => void = () => {}
    const real = api.getMcpAuth.bind(api)
    vi.spyOn(api, 'getMcpAuth').mockImplementation(
      () => new Promise((resolve) => (release = () => resolve(real()))),
    )
    setMcpAuthMode('disabled')
    act(() => mcpAuthChanged())
    await waitFor(() => expect(api.getMcpAuth).toHaveBeenCalled())
    expect(screen.getByTestId('mcp-snippet-claude-code')).toBeInTheDocument()
    release()
    await waitFor(() => expect(snippet()).not.toContain('Authorization'))
  })

  it("takes the Tokens card's mode and asks for none of its own while the card is on the page (#781)", async () => {
    setMcpAuthMode('bearer')
    renderSettingsSection(
      <>
        <McpAccessTokens />
        <McpConnect />
      </>,
      { route },
    )
    await waitFor(() => expect(snippet()).toContain('Authorization'))
    const read = vi.spyOn(api, 'getMcpAuth')
    setMcpAuthMode('disabled')
    act(() => mcpAuthChanged())
    await waitFor(() => expect(snippet()).not.toContain('Authorization'))
    // The card's read alone: the snippet made none.
    expect(read).toHaveBeenCalledTimes(1)
  })

  it("asks for no mode of its own while the Tokens card is on the page but its mode is not known (#5016)", async () => {
    setMcpAuthMode('bearer')
    const read = vi.spyOn(api, 'getMcpAuth').mockImplementation(() => new Promise(() => {}))
    vi.spyOn(api, 'listMcpTokens').mockImplementation(() => new Promise(() => {}))
    renderSettingsSection(
      <>
        <McpAccessTokens />
        <McpConnect />
      </>,
      { route },
    )
    await waitFor(() => expect(read).toHaveBeenCalled())
    // Let the settings load and the snippet mount, then count: the card's read alone.
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(read).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('mcp-snippet-claude-code')).toBeNull()
  })

  it('keeps the previous mode while the Tokens card reads a saved one again (#5016)', async () => {
    setMcpAuthMode('bearer')
    const list = await api.listMcpTokens()
    // The list carries a different mode, so a fall back to it would show on screen.
    vi.spyOn(api, 'listMcpTokens').mockResolvedValue({ ...list, auth_mode: 'disabled' })
    renderSettingsSection(
      <>
        <McpAccessTokens />
        <McpConnect />
      </>,
      { route },
    )
    await waitFor(() => expect(snippet()).toContain('Authorization'))
    vi.spyOn(api, 'getMcpAuth').mockImplementation(() => new Promise(() => {}))
    act(() => mcpAuthChanged())
    await waitFor(() => expect(api.getMcpAuth).toHaveBeenCalled())
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(snippet()).toContain('Authorization')
  })

  it('reads the mode itself when the Tokens card is not on the page (#781)', async () => {
    setMcpAuthMode('bearer')
    renderSettingsSection(<McpConnect />, { route })
    await waitFor(() => expect(snippet()).toContain('Authorization'))
    const read = vi.spyOn(api, 'getMcpAuth')
    setMcpAuthMode('disabled')
    act(() => mcpAuthChanged())
    await waitFor(() => expect(snippet()).not.toContain('Authorization'))
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('falls back to the mode the token list reports when the auth read fails', async () => {
    setMcpAuthMode('disabled')
    vi.spyOn(api, 'getMcpAuth').mockRejectedValue(new Error('down'))
    renderSettingsSection(<McpConnect />, { route })
    await waitFor(() => expect(snippet()).toBe("claude mcp add --transport http scadbuddy 'https://scadbuddy.example/mcp'"))
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
