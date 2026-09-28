import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { setMcpAuthMode } from '../mocks/mcpTokens'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { McpAuthSection } from './McpAuthSection'

// #251 — Settings → "MCP authentication" over the msw stand-in for the agent
// service's /api/v1/ai/mcp/auth (src/mocks/mcpTokens.ts).

async function loaded() {
  return await screen.findByRole('radio', { name: /Require an access token/ })
}

describe('McpAuthSection', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the stored mode and cap, with Save off until something changes', async () => {
    renderPage(<McpAuthSection />)
    expect(await loaded()).toBeChecked()
    expect(screen.getByRole('radio', { name: /Allow calls without a token/ })).not.toBeChecked()
    expect(screen.getByLabelText('Access without a token')).toHaveValue('outward')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull()
  })

  it('asks for confirmation before turning authentication off, and saves nothing on Cancel', async () => {
    const set = vi.spyOn(api, 'setMcpAuth')
    const { user } = renderPage(<McpAuthSection />)
    await loaded()
    await user.click(screen.getByRole('radio', { name: /Allow calls without a token/ }))
    await user.selectOptions(screen.getByLabelText('Access without a token'), 'read')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('dialog', { name: 'Allow MCP calls without a token?' })
    expect(dialog).toHaveTextContent('as an anonymous caller with read only')
    expect(dialog).toHaveTextContent('still wait for a person to approve them')
    expect(set).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(set).not.toHaveBeenCalled()
    expect(await api.getMcpAuth()).toEqual({ mode: 'bearer', anonymous_cap: 'outward' })
  })

  it('turns authentication off once confirmed, then warns while it is off', async () => {
    const onSaved = vi.fn()
    const set = vi.spyOn(api, 'setMcpAuth')
    const { user } = renderPage(<McpAuthSection onSaved={onSaved} />)
    await loaded()
    await user.click(screen.getByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Turn authentication off' }))

    expect(set).toHaveBeenCalledWith({ mode: 'disabled', anonymous_cap: 'outward' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByRole('status')).toHaveTextContent('Saved. It applies from the next MCP call.')
    expect(screen.getByTestId('mcp-auth-disabled-warning')).toHaveTextContent(
      'without a token, with full access',
    )
    expect(onSaved).toHaveBeenCalledWith({ mode: 'disabled', anonymous_cap: 'outward' })
    expect(await api.getMcpAuth()).toEqual({ mode: 'disabled', anonymous_cap: 'outward' })

    // Lowering the cap while off, and turning it back on, need no confirmation.
    await user.selectOptions(screen.getByLabelText('Access without a token'), 'write')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(set).toHaveBeenLastCalledWith({ mode: 'disabled', anonymous_cap: 'write' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await user.click(screen.getByRole('radio', { name: /Require an access token/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(set).toHaveBeenLastCalledWith({ mode: 'bearer', anonymous_cap: 'write' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull())
  })

  it('shows the warning when authentication is already off', async () => {
    setMcpAuthMode('disabled')
    renderPage(<McpAuthSection />)
    expect(await screen.findByTestId('mcp-auth-disabled-warning')).toHaveTextContent('Authentication is off')
    expect(screen.getByRole('radio', { name: /Allow calls without a token/ })).toBeChecked()
  })

  it('says when OIDC is on, and selects neither mode until one is chosen', async () => {
    setMcpAuthMode('oidc')
    renderPage(<McpAuthSection />)
    expect(await screen.findByTestId('mcp-auth-oidc-note')).toHaveTextContent('identity provider')
    expect(await loaded()).not.toBeChecked()
    expect(screen.getByRole('radio', { name: /Allow calls without a token/ })).not.toBeChecked()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('shows the service’s refusal in the dialog and keeps it open', async () => {
    server.use(
      http.put('/api/v1/ai/mcp/auth', () =>
        HttpResponse.json({ detail: 'MCP auth changes must come through the HTTPS ingress' }, { status: 403 }),
      ),
    )
    const { user } = renderPage(<McpAuthSection />)
    await loaded()
    await user.click(screen.getByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Turn authentication off' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('must come through the HTTPS ingress')
    expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull()
  })

  it('says so when the setting cannot be loaded', async () => {
    server.use(
      http.get('/api/v1/ai/mcp/auth', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<McpAuthSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('AI features need the database')
  })
})
