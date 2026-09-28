import { screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { McpOidcSettings, OIDC_API } from './McpOidcSettings'

// The Settings section for /mcp OIDC (#262), against src/mocks/mcpOidc.ts.

describe('McpOidcSettings', () => {
  it('shows the stored configuration and the resource clients must ask for', async () => {
    renderPage(<McpOidcSettings />)
    expect(await screen.findByLabelText('Issuer URL')).toHaveValue('https://idp.example.com/')
    expect(screen.getByLabelText('Audience')).toHaveAttribute('placeholder', 'https://scadbuddy.example/mcp')
    expect(screen.getByLabelText('Scope for write')).toHaveValue('scadbuddy:write')
    expect(screen.getByRole('checkbox', { name: 'RS256' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'EdDSA' })).not.toBeChecked()
    expect(screen.getByText('https://scadbuddy.example/.well-known/oauth-protected-resource')).toBeInTheDocument()
  })

  it('tests discovery and reports whether clients can register themselves', async () => {
    const { user } = renderPage(<McpOidcSettings />)
    await screen.findByLabelText('Issuer URL')
    await user.click(screen.getByRole('button', { name: 'Test discovery' }))
    expect(await screen.findByRole('status')).toHaveTextContent('MCP clients can register themselves')
  })

  it('enables OIDC and sends the whole configuration', async () => {
    let sent: unknown
    server.use(
      http.put(OIDC_API, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({
          config: sent,
          saved: true,
          resource: 'https://scadbuddy.example/mcp',
          resource_metadata_url: null,
          supported_algorithms: ['RS256', 'ES256'],
          can_enable: true,
          cannot_enable_reason: null,
        })
      }),
    )
    const { user } = renderPage(<McpOidcSettings />)
    const issuer = await screen.findByLabelText('Issuer URL')
    await user.clear(issuer)
    await user.type(issuer, 'https://auth.home.example/application/o/scadbuddy/')
    await user.type(screen.getByLabelText('Also read tiers from claim (optional)'), 'groups')
    await user.click(screen.getByRole('checkbox', { name: /Let MCP clients sign in/ }))
    await user.click(screen.getByRole('button', { name: 'Save sign-in settings' }))
    expect(await screen.findByRole('status')).toHaveTextContent('now accepts sign-ins')
    expect(sent).toMatchObject({
      enabled: true,
      issuer: 'https://auth.home.example/application/o/scadbuddy/',
      audience: null,
      tier_claim: 'groups',
      algorithms: ['RS256', 'ES256'],
    })
  })

  it('shows why enabling was refused', async () => {
    const { user } = renderPage(<McpOidcSettings />)
    const issuer = await screen.findByLabelText('Issuer URL')
    await user.clear(issuer)
    await user.type(issuer, 'https://typo.example/')
    await user.click(screen.getByRole('checkbox', { name: /Let MCP clients sign in/ }))
    await user.click(screen.getByRole('button', { name: 'Save sign-in settings' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('nothing was saved')
  })
})
