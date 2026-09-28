import { describe, expect, it } from 'vitest'
import { EgressError } from '../src/http/egress.js'
import { decide } from '../src/harness/permissions.js'
import {
  buildHarnessOptions,
  harnessTierOf,
  PluginConfigError,
  pluginHeaderEnv,
  remotePluginOptions,
} from '../src/harness/run.js'
import {
  assertEndpointAllowed,
  normalisePluginUrl,
  PluginError,
  pluginTierResolver,
  type RemotePlugin,
  validateDisabledTools,
  validateHeaderName,
  validatePluginName,
  validateToolTiers,
} from '../src/plugins/registry.js'
import { describeTools } from '../src/plugins/testConnection.js'

// The registry's pure rules (#297): names, URLs, the endpoint egress check,
// tier defaults and overrides, and the SDK options a plugin becomes. The
// Postgres store is test/plugins.pg.test.ts; the real SDK end to end is
// test/plugins.e2e.test.ts.

const TOKEN = 'hs-unit-test-token-0000aaaabbbbcccc'
const memory: RemotePlugin = {
  name: 'memory',
  url: 'https://memory.example/mcp/bank/',
  header: { name: 'Authorization', value: `Bearer ${TOKEN}` },
  toolTiers: { recall: 'read', note: 'write' },
  disabledTools: ['delete_bank'],
}

describe('plugin names', () => {
  it.each(['memory', 'hindsight', 'my-memory', 'a1', 'x'.repeat(32)])('accepts %s', (name) => {
    expect(validatePluginName(name)).toBe(name)
  })
  it.each(['', 'a', 'Memory', '1mem', 'mem_ory', 'mem--ory', 'mem-', 'x'.repeat(33), 'mem.ory', 'mem ory'])(
    'refuses %j',
    (name) => {
      expect(() => validatePluginName(name)).toThrow(PluginError)
    },
  )
  it('refuses the reserved server names', () => {
    for (const name of ['scadbuddy', 'playwright']) expect(() => validatePluginName(name)).toThrow(/reserved/)
  })
})

describe('plugin URLs', () => {
  it('keeps the path as given, trailing slash included', () => {
    expect(normalisePluginUrl(' https://hs.example/mcp/bank-1/ ')).toBe('https://hs.example/mcp/bank-1/')
  })
  it.each([
    ['not a URL', 'nope'],
    ['a non-http scheme', 'ftp://hs.example/mcp'],
    ['credentials', 'https://user:pw@hs.example/mcp'],
    ['a query (tokens belong in the sealed header)', 'https://hs.example/mcp?token=abc'],
    ['a fragment', 'https://hs.example/mcp#x'],
    ['a $ (Claude Code expands ${VAR} in MCP config)', 'https://hs.example/mcp/$HOME'],
  ])('refuses %s', (_name, url) => {
    expect(() => normalisePluginUrl(url)).toThrow(PluginError)
  })
})

describe('endpoint egress check (https unless loopback; no link-local or metadata)', () => {
  const resolveTo = (addresses: string[]) => () => Promise.resolve(addresses)

  it.each([
    ['the metadata address', 'https://169.254.169.254/mcp', ['169.254.169.254']],
    ['a name resolving to link-local', 'https://hs.example/mcp', ['203.0.113.5', '169.254.10.1']],
    ["GCP's metadata name", 'https://metadata.google.internal/mcp', ['169.254.169.254']],
    ['IPv6 link-local', 'https://[fe80::1]/mcp', []],
    ['plain http to a LAN host', 'http://10.1.2.3:8888/mcp/', ['10.1.2.3']],
    ['plain http to a public name', 'http://hs.example/mcp/', ['203.0.113.5']],
    ['plain http to a name resolving to loopback AND LAN', 'http://mixed.example/mcp/', ['127.0.0.1', '10.0.0.5']],
  ])('refuses %s', async (_name, url, addresses) => {
    await expect(assertEndpointAllowed(url, resolveTo(addresses))).rejects.toThrow(EgressError)
  })

  it('refuses an unresolvable host', async () => {
    await expect(assertEndpointAllowed('https://nx.example/mcp', () => Promise.reject(new Error('ENOTFOUND')))).rejects.toThrow(
      /cannot be resolved/,
    )
  })

  it.each([
    ['https to a public host', 'https://hs.example/mcp/', ['203.0.113.5']],
    ['https to a LAN host', 'https://10.1.2.3/mcp/', ['10.1.2.3']],
    ['http to 127.0.0.1', 'http://127.0.0.1:8888/mcp/', []],
    ['http to [::1]', 'http://[::1]:8888/mcp/', []],
    ['http to localhost resolving to loopback', 'http://localhost:8888/mcp/', ['127.0.0.1', '::1']],
  ])('allows %s', async (_name, url, addresses) => {
    await expect(assertEndpointAllowed(url, resolveTo(addresses))).resolves.toBeUndefined()
  })
})

describe('tiers: unknown plugin tools are outward; explicit entries win', () => {
  const tierOf = pluginTierResolver([memory])

  it('uses the explicit tier', () => {
    expect(tierOf('mcp__memory__recall')).toBe('read')
    expect(tierOf('mcp__memory__note')).toBe('write')
  })

  it('defaults every other tool of the plugin to outward (spec §8.1), which needs approval', () => {
    expect(tierOf('mcp__memory__retain')).toBe('outward')
    expect(decide('mcp__memory__retain', tierOf)).toMatchObject({ decision: 'needs_approval', tier: 'outward' })
    // Not fooled by an inherited property name.
    expect(tierOf('mcp__memory__toString')).toBe('outward')
    expect(tierOf('mcp__memory__constructor')).toBe('outward')
  })

  it('answers only for its own prefix', () => {
    expect(tierOf('mcp__scadbuddy__render_model')).toBeUndefined()
    expect(tierOf('mcp__memoryx__recall')).toBeUndefined()
    expect(tierOf('Bash')).toBeUndefined()
  })

  it('takes precedence over the run tier resolver for plugin tools only', () => {
    const combined = harnessTierOf({ remotePlugins: [memory], tierOf: () => 'read' })
    expect(combined('mcp__memory__retain')).toBe('outward')
    expect(combined('mcp__scadbuddy__render_model')).toBe('read')
    expect(harnessTierOf({ remotePlugins: [memory] })('mcp__scadbuddy__x')).toBeUndefined()
  })

  it('validates tier maps and the disabled list', () => {
    expect(validateToolTiers({ recall: 'read' })).toEqual({ recall: 'read' })
    expect(() => validateToolTiers({ recall: 'admin' })).toThrow(/must be one of read, write, outward/)
    expect(() => validateToolTiers({ 'bad name': 'read' })).toThrow(PluginError)
    expect(validateDisabledTools(['b', 'a', 'b'])).toEqual(['a', 'b'])
    expect(() => validateDisabledTools(['a/b'])).toThrow(PluginError)
  })

  it('describes listed tools with tier, source and the annotation only as a suggestion', () => {
    const tools = describeTools({ name: 'memory', toolTiers: { note: 'write' }, disabledTools: ['wipe'] }, [
      { name: 'recall', annotations: { readOnlyHint: true } },
      { name: 'note' },
      { name: 'wipe', annotations: { destructiveHint: true } },
    ])
    expect(tools.map((t) => [t.name, t.tier, t.tier_source, t.suggested_tier, t.disabled])).toEqual([
      // readOnlyHint does NOT lower the tier; it only pre-fills the review.
      ['recall', 'outward', 'default', 'read', false],
      ['note', 'write', 'explicit', null, false],
      ['wipe', 'outward', 'default', null, true],
    ])
  })
})

describe('auth header names', () => {
  it('accepts ordinary header names and refuses transport ones', () => {
    expect(validateHeaderName('Authorization')).toBe('Authorization')
    expect(validateHeaderName('X-Api-Key')).toBe('X-Api-Key')
    for (const bad of ['Host', 'content-type', 'Mcp-Session-Id', 'bad header', 'a:b', '']) {
      expect(() => validateHeaderName(bad)).toThrow(PluginError)
    }
  })
})

describe('the SDK options a plugin becomes', () => {
  const paths = { stateDir: '/var/lib/scadbuddy-agent' }
  const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-api03-unit-0000' }

  it('is a Streamable HTTP server whose header references an env var holding the value', () => {
    const { mcpServers, env, disallowedTools } = remotePluginOptions([memory, { ...memory, name: 'other', header: undefined }], new Set())
    expect(mcpServers).toEqual({
      memory: {
        type: 'http',
        url: 'https://memory.example/mcp/bank/',
        alwaysLoad: true,
        headers: { Authorization: `\${${pluginHeaderEnv(0)}}` },
      },
      other: { type: 'http', url: 'https://memory.example/mcp/bank/', alwaysLoad: true },
    })
    expect(env).toEqual({ [pluginHeaderEnv(0)]: `Bearer ${TOKEN}` })
    expect(disallowedTools).toEqual(['mcp__memory__delete_bank', 'mcp__other__delete_bank'])
  })

  it('refuses a plugin whose name is already an in-process server, or used twice', () => {
    expect(() => remotePluginOptions([memory], new Set(['memory']))).toThrow(PluginConfigError)
    expect(() => remotePluginOptions([memory, memory], new Set())).toThrow(PluginConfigError)
  })

  it('wires into the harness options: servers, env, disallowedTools; the value only in env', () => {
    const options = buildHarnessOptions({ paths, credential, prompt: 'hi', remotePlugins: [memory] })
    expect(Object.keys(options.mcpServers ?? {})).toEqual(['memory'])
    expect(options.disallowedTools).toEqual(['mcp__memory__delete_bank'])
    expect(options.env?.[pluginHeaderEnv(0)]).toBe(`Bearer ${TOKEN}`)
    expect(options.tools).toEqual([])
    const { env: _env, ...rest } = options
    expect(JSON.stringify(rest)).not.toContain(TOKEN)
  })

  it('cannot shadow the credential variables', () => {
    const options = buildHarnessOptions({ paths, credential, prompt: 'hi', remotePlugins: [memory] })
    expect(options.env?.ANTHROPIC_API_KEY).toBe(credential.secret)
  })

  it('redacts plugin header values from stderr', () => {
    const lines: string[] = []
    const options = buildHarnessOptions({
      paths,
      credential,
      prompt: 'hi',
      remotePlugins: [memory],
      stderr: (l) => lines.push(l),
    })
    options.stderr?.(`MCP error with Bearer ${TOKEN}\n`)
    expect(lines.join('')).not.toContain(TOKEN)
  })
})
