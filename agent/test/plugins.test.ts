import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { assertPluginAllowed, PluginRefusedError, pluginProblems } from '../src/harness/plugins.js'

// Fixtures in test/fixtures/plugins/, one per way a plugin can start a process
// (src/harness/plugins.ts cites the manifest format).
const fixture = (name: string) => path.join(import.meta.dirname, 'fixtures', 'plugins', name)

describe('plugin vetting (spec §8.6, "command hooks refused")', () => {
  it('allows skills, remote MCP servers and non-command hooks', () => {
    expect(pluginProblems(fixture('clean'))).toEqual([])
    expect(() => assertPluginAllowed(fixture('clean'))).not.toThrow()
  })

  it("allows ScadBuddy's own plugin (its .mcp.json is an http server)", () => {
    expect(pluginProblems(path.join(import.meta.dirname, '..', '..', 'plugins', 'scadbuddy'))).toEqual([])
  })

  it('refuses a command hook in hooks/hooks.json', () => {
    expect(pluginProblems(fixture('command-hook'))).toEqual([
      'hooks/hooks.json: SessionStart has a "command" hook',
    ])
  })

  it('refuses a hook without a type in a file the manifest names, and keeps the inline prompt hook', () => {
    expect(pluginProblems(fixture('inline-command-hook'))).toEqual([
      './config/extra-hooks.json: Stop has a command hook',
    ])
  })

  it('refuses a stdio MCP server in .mcp.json', () => {
    expect(pluginProblems(fixture('stdio-mcp'))).toEqual(['.mcp.json: MCP server "local" is a local (stdio) server'])
  })

  it('refuses stdio servers, commands and bundles declared in the manifest', () => {
    expect(pluginProblems(fixture('manifest-mcp'))).toEqual([
      'plugin.json mcpServers: MCP server "typed-stdio" is a local (stdio) server',
      'plugin.json mcpServers: MCP server "http-with-command" is a local (stdio) server',
      'mcpServers: ./bundle/server.mcpb is a bundle, which runs a local server',
    ])
  })

  it('refuses LSP servers and monitors', () => {
    expect(pluginProblems(fixture('lsp-and-monitors'))).toEqual([
      'declares LSP servers, which run local commands',
      'declares monitors, which run local commands',
    ])
  })

  it('refuses a path that is not a plugin directory', () => {
    expect(pluginProblems(fixture('does-not-exist'))).toEqual(['not a directory'])
  })

  it('names every problem in the error', () => {
    try {
      assertPluginAllowed(fixture('manifest-mcp'))
      expect.unreachable()
    } catch (err) {
      expect(err).toBeInstanceOf(PluginRefusedError)
      expect((err as PluginRefusedError).problems).toHaveLength(3)
      expect((err as Error).message).toMatch(/typed-stdio.*http-with-command.*server\.mcpb/)
    }
  })
})
