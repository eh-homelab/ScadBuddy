import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../test/utils'
import { McpConnectSnippet } from './McpConnectSnippet'

const code = () => screen.getByTestId('mcp-snippet-claude-code').textContent ?? ''
const desktop = () => JSON.parse(screen.getByTestId('mcp-snippet-claude-desktop').textContent ?? '') as {
  mcpServers: { scadbuddy: { command: string; args: string[]; env?: Record<string, string> } }
}

describe('McpConnectSnippet (#1910)', () => {
  it('builds both snippets from the public URL, with a placeholder for the token', () => {
    renderPage(<McpConnectSnippet publicUrl="https://scadbuddy.example/" authMode="bearer" />)
    expect(code()).toBe(
      'claude mcp add --transport http scadbuddy https://scadbuddy.example/mcp --header "Authorization: Bearer <your token>"',
    )
    expect(desktop()).toEqual({
      mcpServers: {
        scadbuddy: {
          command: 'npx',
          args: ['-y', 'mcp-remote', 'https://scadbuddy.example/mcp', '--header', 'Authorization:${SCADBUDDY_AUTH}'],
          env: { SCADBUDDY_AUTH: 'Bearer <your token>' },
        },
      },
    })
    expect(screen.getByText(/Create a token below/)).toBeInTheDocument()
  })

  it('fills in a token just minted', () => {
    renderPage(<McpConnectSnippet publicUrl="https://scadbuddy.example" authMode="bearer" token="sbmcp_abc" />)
    expect(code()).toContain('--header "Authorization: Bearer sbmcp_abc"')
    expect(desktop().mcpServers.scadbuddy.env).toEqual({ SCADBUDDY_AUTH: 'Bearer sbmcp_abc' })
    expect(screen.queryByText(/Create a token below/)).not.toBeInTheDocument()
  })

  it('keeps the header while OIDC is on: bearer tokens still work', () => {
    renderPage(<McpConnectSnippet publicUrl="https://scadbuddy.example" authMode="oidc" token="sbmcp_abc" />)
    expect(code()).toContain('Authorization: Bearer sbmcp_abc')
  })

  it('sends no header while MCP authentication is off', () => {
    renderPage(<McpConnectSnippet publicUrl="https://scadbuddy.example" authMode="disabled" token="sbmcp_abc" />)
    expect(code()).toBe('claude mcp add --transport http scadbuddy https://scadbuddy.example/mcp')
    expect(desktop()).toEqual({
      mcpServers: { scadbuddy: { command: 'npx', args: ['-y', 'mcp-remote', 'https://scadbuddy.example/mcp'] } },
    })
    expect(screen.getByTestId('mcp-snippet')).not.toHaveTextContent('Authorization')
  })

  it("uses this page's origin when no public URL is set", () => {
    renderPage(<McpConnectSnippet publicUrl={null} authMode="bearer" />)
    expect(code()).toContain(`${window.location.origin}/mcp`)
  })

  it('links to the plugin install, and keeps the copy buttons for the user', () => {
    renderPage(<McpConnectSnippet publicUrl="https://scadbuddy.example" authMode="bearer" />)
    expect(screen.getByRole('link', { name: /ScadBuddy Claude plugin/ })).toHaveAttribute(
      'href',
      'https://github.com/eh-homelab/ScadBuddy/blob/main/docs/ai/claude-plugin.md',
    )
    for (const name of ['Copy the Claude Code command', 'Copy the Claude Desktop config']) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('data-agent-user-only')
    }
  })
})
