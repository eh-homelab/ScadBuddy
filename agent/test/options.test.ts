import { describe, expect, it } from 'vitest'
import {
  buildQueryOptions,
  claudeConfigDir,
  DEFAULT_STATE_DIR,
  scratchDir,
} from '../src/harness/options.js'

describe('buildQueryOptions', () => {
  const paths = { stateDir: DEFAULT_STATE_DIR }
  const options = buildQueryOptions(paths)

  it('removes every built-in tool (spec D7)', () => {
    expect(options.tools).toEqual([])
  })

  it('loads no filesystem settings and no ambient MCP config', () => {
    expect(options.settingSources).toEqual([])
    expect(options.strictMcpConfig).toBe(true)
    expect(options.mcpServers).toEqual({})
  })

  it('points Claude Code at the service-owned directories', () => {
    expect(claudeConfigDir(paths)).toBe('/var/lib/scadbuddy-agent/claude')
    expect(scratchDir(paths)).toBe('/var/lib/scadbuddy-agent/work')
    expect(options.cwd).toBe(scratchDir(paths))
    expect(options.env?.CLAUDE_CONFIG_DIR).toBe(claudeConfigDir(paths))
  })

  it('does not hand the service environment to the subprocess', () => {
    // `env` REPLACES the subprocess environment (sdk.d.ts), so only the keys
    // listed here reach Claude Code; the database URL must not be one.
    expect(Object.keys(options.env ?? {}).sort()).toEqual(
      ['CLAUDE_CONFIG_DIR', 'HOME', ...(process.env.PATH === undefined ? [] : ['PATH'])].sort(),
    )
  })

  it('turns off inline shell execution in skills and commands', () => {
    expect(options.settings).toEqual({ disableSkillShellExecution: true })
  })

  it('grants no tool permissions up front', () => {
    expect(options.allowedTools).toBeUndefined()
    expect(options.permissionMode).toBeUndefined()
    expect(options.plugins).toBeUndefined()
  })
})
