import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { setMcpAuthMode } from '../mocks/mcpTokens'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { McpTokensSection } from './McpTokensSection'

async function listed() {
  return await screen.findByRole('list', { name: 'Tokens' })
}

describe('McpTokensSection', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('lists tokens with their metadata and no secret', async () => {
    renderPage(<McpTokensSection />)
    const list = await listed()
    const rows = within(list).getAllByTestId('mcp-token')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Claude Desktop')
    expect(rows[0]).toHaveTextContent('Outward')
    expect(rows[0]).toHaveTextContent('Created 2026-09-20')
    expect(rows[0]).toHaveTextContent('Never expires')
    expect(rows[0]).toHaveTextContent('Last used')
    expect(rows[1]).toHaveTextContent('Revoked 2026-08-15')
    expect(rows[1]).toHaveTextContent('Never used')
    // A revoked token cannot be revoked again.
    expect(within(rows[1]!).queryByRole('button')).toBeNull()
    expect(document.body.textContent).not.toMatch(/sbmcp_/)
  })

  it('creates a token and shows the plaintext once, with a warning', async () => {
    const create = vi.spyOn(api, 'createMcpToken')
    const { user } = renderPage(<McpTokensSection />)
    await listed()

    const button = screen.getByRole('button', { name: 'Create token' })
    expect(button).toBeDisabled()
    await user.type(screen.getByLabelText('Token name'), '  Claude Code  ')
    await user.selectOptions(screen.getByLabelText('Access'), 'write')
    await user.selectOptions(screen.getByLabelText('Expires after'), '30')
    await user.click(button)

    expect(create).toHaveBeenCalledWith({ name: 'Claude Code', tier: 'write', expires_in: 30 * 86400 })
    const panel = await screen.findByTestId('minted-token')
    expect(panel).toHaveTextContent('New token: Claude Code')
    expect(panel).toHaveTextContent('This is the only time it is shown')
    const value = within(panel).getByTestId('minted-token-value')
    expect(value.textContent).toMatch(/^sbmcp_[A-Za-z0-9_-]{43}$/)
    // Rendered as text, not a field value the browser agent's snapshot would read.
    expect(value.tagName).toBe('CODE')
    expect(screen.getByLabelText('Token name')).toHaveValue('')

    // The list picks it up, as metadata.
    await waitFor(() => expect(within(screen.getByRole('list')).getAllByTestId('mcp-token')).toHaveLength(3))
    const token = value.textContent!
    expect(screen.getByRole('list').textContent).not.toContain(token)

    // Done forgets it: nothing can show it again.
    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByTestId('minted-token')).toBeNull()
    expect(document.body.textContent).not.toContain(token)
  })

  it('copies the token with the Clipboard API', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    const { user } = renderPage(<McpTokensSection />)
    // user-event installs its own clipboard stub at setup; replace it after.
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    await listed()
    await user.type(screen.getByLabelText('Token name'), 'ci')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const value = await screen.findByTestId('minted-token-value')

    await user.click(screen.getByRole('button', { name: 'Copy token' }))
    expect(writeText).toHaveBeenCalledWith(value.textContent)
    expect(await screen.findByRole('status')).toHaveTextContent('Copied to the clipboard.')
  })

  it('in a frame that may not write the clipboard, selects the token for a manual copy', async () => {
    const { user } = renderPage(<McpTokensSection />)
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')) },
    })
    Object.defineProperty(document, 'execCommand', { value: () => false, configurable: true })
    await listed()
    await user.type(screen.getByLabelText('Token name'), 'ci')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const value = await screen.findByTestId('minted-token-value')

    await user.click(screen.getByRole('button', { name: 'Copy token' }))
    expect(await screen.findByRole('status')).toHaveTextContent('press Ctrl+C')
    expect(window.getSelection()?.toString()).toBe(value.textContent)
  })

  it('shows why a create was refused', async () => {
    server.use(
      http.post('/api/v1/ai/mcp-tokens', () =>
        HttpResponse.json(
          { detail: 'MCP token changes must come from the ScadBuddy UI (no Origin header)' },
          { status: 403 },
        ),
      ),
    )
    const { user } = renderPage(<McpTokensSection />)
    await listed()
    await user.type(screen.getByLabelText('Token name'), 'x')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no Origin header')
    expect(screen.queryByTestId('minted-token')).toBeNull()
  })

  it('revokes after a confirmation, and can be cancelled', async () => {
    const revoke = vi.spyOn(api, 'revokeMcpToken')
    const { user } = renderPage(<McpTokensSection />)
    await listed()

    await user.click(screen.getByRole('button', { name: 'Revoke Claude Desktop' }))
    let dialog = screen.getByRole('dialog', { name: 'Revoke Claude Desktop?' })
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(revoke).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Revoke Claude Desktop' }))
    dialog = screen.getByRole('dialog', { name: 'Revoke Claude Desktop?' })
    await user.click(within(dialog).getByRole('button', { name: 'Revoke token' }))
    expect(revoke).toHaveBeenCalledWith('6f1c2b1e-0d5c-4c7e-9a51-3b2f0b6f1a01')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Revoke Claude Desktop' })).toBeNull())
    expect(screen.getAllByTestId('mcp-token')[0]).toHaveTextContent(/Revoked \d{4}-\d{2}-\d{2}/)
  })

  it('keeps the dialog open with the reason when a revoke fails', async () => {
    server.use(
      http.delete('/api/v1/ai/mcp-tokens/:id', () =>
        HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 }),
      ),
    )
    const { user } = renderPage(<McpTokensSection />)
    await listed()
    await user.click(screen.getByRole('button', { name: 'Revoke Claude Desktop' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Revoke token' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('the AI database is unreachable')
  })

  it('warns when MCP authentication is off, and notes OIDC', async () => {
    setMcpAuthMode('disabled')
    const first = renderPage(<McpTokensSection />)
    expect(await screen.findByTestId('mcp-auth-note')).toHaveTextContent('authentication is turned off')
    first.unmount()

    setMcpAuthMode('oidc')
    renderPage(<McpTokensSection />)
    expect(await screen.findByTestId('mcp-auth-note')).toHaveTextContent('keep working alongside')
  })

  it('says why when the AI service cannot list tokens', async () => {
    server.use(
      http.get('/api/v1/ai/mcp-tokens', () =>
        HttpResponse.json(
          { detail: 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)' },
          { status: 503 },
        ),
      ),
    )
    renderPage(<McpTokensSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('SCADBUDDY_DATABASE_URL is not set')
  })

  it('says so when there are no tokens', async () => {
    server.use(http.get('/api/v1/ai/mcp-tokens', () => HttpResponse.json({ auth_mode: 'bearer', tokens: [] })))
    renderPage(<McpTokensSection />)
    expect(await screen.findByText('No tokens yet.')).toBeInTheDocument()
  })
})
