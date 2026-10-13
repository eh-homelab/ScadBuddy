import { screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { setMcpAuthMode } from '../mocks/features/mcpTokens'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { McpAuthBanner } from './McpAuthBanner'

const settle = () => new Promise((r) => setTimeout(r, 50))

describe('McpAuthBanner (#1921)', () => {
  it('says nothing while /mcp needs a token', async () => {
    renderPage(<McpAuthBanner />)
    await settle()
    expect(screen.queryByTestId('mcp-auth-banner')).not.toBeInTheDocument()
  })

  it('says nothing while OIDC overrides a stored "disabled"', async () => {
    setMcpAuthMode('oidc', 'outward', 'disabled')
    renderPage(<McpAuthBanner />)
    await settle()
    expect(screen.queryByTestId('mcp-auth-banner')).not.toBeInTheDocument()
  })

  it('warns while auth is disabled, naming the anonymous cap', async () => {
    setMcpAuthMode('disabled', 'write')
    renderPage(<McpAuthBanner />)
    const banner = await screen.findByTestId('mcp-auth-banner')
    expect(banner).toHaveAttribute('role', 'alert')
    expect(banner).toHaveTextContent('MCP authentication is off')
    expect(banner).toHaveTextContent('with read and write access')
    // Persistent: nothing dismisses it.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('links to the setting from the assistant panel', async () => {
    setMcpAuthMode('disabled', 'outward')
    renderPage(<McpAuthBanner link />)
    const banner = await screen.findByTestId('mcp-auth-banner')
    expect(banner).toHaveTextContent('with full access')
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings#assistant')
  })

  it('says nothing when the agent cannot answer', async () => {
    server.use(http.get('/api/v1/ai/mcp/auth', () => HttpResponse.json({ detail: 'no database' }, { status: 503 })))
    renderPage(<McpAuthBanner />)
    await settle()
    expect(screen.queryByTestId('mcp-auth-banner')).not.toBeInTheDocument()
  })
})
