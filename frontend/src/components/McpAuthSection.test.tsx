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
    expect(await api.getMcpAuth()).toEqual({
      mode: 'bearer',
      configured_mode: 'bearer',
      anonymous_cap: 'outward',
    })
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

    expect(set).toHaveBeenCalledWith({
      mode: 'disabled',
      anonymous_cap: 'outward',
      expected: { mode: 'bearer', anonymous_cap: 'outward' },
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByRole('status')).toHaveTextContent('Saved. It applies from the next MCP call.')
    expect(screen.getByTestId('mcp-auth-disabled-warning')).toHaveTextContent(
      'without a token, with full access',
    )
    const off = { mode: 'disabled', configured_mode: 'disabled', anonymous_cap: 'outward' }
    expect(onSaved).toHaveBeenCalledWith(off)
    expect(await api.getMcpAuth()).toEqual(off)

    // Lowering the cap while off, and turning it back on, need no confirmation.
    await user.selectOptions(screen.getByLabelText('Access without a token'), 'write')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(set).toHaveBeenLastCalledWith({
        mode: 'disabled',
        anonymous_cap: 'write',
        expected: { mode: 'disabled', anonymous_cap: 'outward' },
      }),
    )
    expect(screen.queryByRole('dialog')).toBeNull()
    await user.click(screen.getByRole('radio', { name: /Require an access token/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(set).toHaveBeenLastCalledWith({
        mode: 'bearer',
        anonymous_cap: 'write',
        expected: { mode: 'disabled', anonymous_cap: 'write' },
      }),
    )
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull())
  })

  it('asks for confirmation before raising the anonymous cap while authentication is off', async () => {
    setMcpAuthMode('disabled', 'read')
    const set = vi.spyOn(api, 'setMcpAuth')
    const { user } = renderPage(<McpAuthSection />)
    expect(await screen.findByTestId('mcp-auth-disabled-warning')).toHaveTextContent('with read only')
    await user.selectOptions(screen.getByLabelText('Access without a token'), 'outward')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('dialog', {
      name: 'Give MCP calls without a token more access?',
    })
    expect(dialog).toHaveTextContent('as an anonymous caller with full access')
    expect(set).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(set).not.toHaveBeenCalled()
    expect(await api.getMcpAuth()).toMatchObject({ mode: 'disabled', anonymous_cap: 'read' })

    await user.click(screen.getByRole('button', { name: 'Save' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Raise access' }))
    expect(set).toHaveBeenCalledWith({
      mode: 'disabled',
      anonymous_cap: 'outward',
      expected: { mode: 'disabled', anonymous_cap: 'read' },
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('mcp-auth-disabled-warning')).toHaveTextContent('with full access')
  })

  it('shows the warning when authentication is already off', async () => {
    setMcpAuthMode('disabled')
    renderPage(<McpAuthSection />)
    expect(await screen.findByTestId('mcp-auth-disabled-warning')).toHaveTextContent('Authentication is off')
    expect(screen.getByRole('radio', { name: /Allow calls without a token/ })).toBeChecked()
  })

  it('says OIDC overrides the stored mode, and shows the stored one', async () => {
    setMcpAuthMode('oidc')
    renderPage(<McpAuthSection />)
    expect(await screen.findByTestId('mcp-auth-oidc-note')).toHaveTextContent(
      'OIDC sign-in is on, so it overrides the choice below',
    )
    expect(await loaded()).toBeChecked()
    expect(screen.queryByTestId('mcp-auth-oidc-disabled')).toBeNull()
    expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('warns that a stored "off" returns with OIDC off, and never says auth is off meanwhile', async () => {
    setMcpAuthMode('oidc', 'read', 'disabled')
    renderPage(<McpAuthSection />)
    expect(await screen.findByTestId('mcp-auth-oidc-disabled')).toHaveTextContent(
      'turning OIDC off will let anyone who can reach /mcp call it with read only',
    )
    expect(screen.getByRole('radio', { name: /Allow calls without a token/ })).toBeChecked()
    expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull()
  })

  it('still asks before storing "off" while OIDC is on, and says OIDC keeps applying', async () => {
    setMcpAuthMode('oidc')
    const onSaved = vi.fn()
    const set = vi.spyOn(api, 'setMcpAuth')
    const { user } = renderPage(<McpAuthSection onSaved={onSaved} />)
    await loaded()
    await user.click(screen.getByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    const dialog = await screen.findByRole('dialog', { name: 'Allow MCP calls without a token?' })
    expect(within(dialog).getByTestId('mcp-auth-confirm-oidc')).toHaveTextContent(
      'OIDC sign-in is on and still applies after this save',
    )
    expect(set).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Allow once OIDC is off' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(onSaved).toHaveBeenCalledWith({
      mode: 'oidc',
      configured_mode: 'disabled',
      anonymous_cap: 'outward',
    })
    expect(screen.getByRole('status')).toHaveTextContent('OIDC sign-in still applies')
    expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull()
    expect(screen.getByTestId('mcp-auth-oidc-disabled')).toBeInTheDocument()
  })

  it('refuses a save from a stale view, reloads, and confirms again against the current setting', async () => {
    // The tab loads while auth is off with read and write access...
    setMcpAuthMode('disabled', 'write')
    const set = vi.spyOn(api, 'setMcpAuth')
    const onSaved = vi.fn()
    const { user } = renderPage(<McpAuthSection onSaved={onSaved} />)
    expect(await screen.findByTestId('mcp-auth-disabled-warning')).toBeInTheDocument()
    // ...then another tab turns it back on.
    setMcpAuthMode('bearer', 'write')

    // Lowering the cap looks safe from the stale view, so no dialog; the service refuses it.
    await user.selectOptions(screen.getByLabelText('Access without a token'), 'read')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('changed elsewhere')
    expect(set).toHaveBeenCalledTimes(1)
    expect(await api.getMcpAuth()).toMatchObject({ mode: 'bearer', anonymous_cap: 'write' })
    expect(onSaved).toHaveBeenLastCalledWith({
      mode: 'bearer',
      configured_mode: 'bearer',
      anonymous_cap: 'write',
    })

    // The page now shows the current setting, and turning auth off asks first.
    await waitFor(() => expect(screen.queryByTestId('mcp-auth-disabled-warning')).toBeNull())
    expect(screen.getByRole('radio', { name: /Require an access token/ })).toBeChecked()
    await user.click(screen.getByRole('radio', { name: /Allow calls without a token/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('dialog', { name: 'Allow MCP calls without a token?' })).toBeInTheDocument()
    expect(set).toHaveBeenCalledTimes(1)
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
