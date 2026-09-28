import { describe, expect, it } from 'vitest'
import { assertGatewayHostAllowed, EgressError, embeddedIPv4, ipv6Bytes } from '../src/http/egress.js'
import { decide } from '../src/harness/permissions.js'
import { buildHarnessOptions, harnessTierOf, PluginConfigError, remotePluginOptions } from '../src/harness/run.js'
import { callRefusal, filterToolList, type HarnessPlugin } from '../src/plugins/forwarder.js'
import {
  assertEndpointAllowed,
  harnessToolName,
  headerSecretVariants,
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
// tool naming, tier defaults and overrides, the forwarder's tool policy, and
// the SDK options a plugin becomes. The Postgres store is
// test/plugins.pg.test.ts; the real SDK end to end is test/plugins.e2e.test.ts.

const TOKEN = 'hs-unit-test-token-0000aaaabbbbcccc'
const memory: RemotePlugin = {
  name: 'memory',
  url: 'https://memory.example/mcp/bank/',
  header: { name: 'Authorization', value: `Bearer ${TOKEN}` },
  toolTiers: { recall: 'read', note: 'write', files_list: 'read' },
  disabledTools: ['delete_bank', 'files.delete', 'my tool'],
}
const forwardedMemory: HarnessPlugin = {
  name: 'memory',
  url: 'http://127.0.0.1:1234/p/abcdefghijklmnopqrstuvwx',
  toolTiers: memory.toolTiers,
  disabledTools: memory.disabledTools,
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
  it.each(['scadbuddy', 'playwright', 'workspace', 'computer-use', 'claude-in-chrome', 'hearthbot', 'ide'])(
    'refuses the reserved server name %s',
    (name) => {
      expect(() => validatePluginName(name)).toThrow(/reserved/)
    },
  )
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
    ['a $', 'https://hs.example/mcp/$HOME'],
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
    ['NAT64 of the metadata address', 'https://[64:ff9b::a9fe:a9fe]/mcp', []],
    ['NAT64 of the metadata address, dotted', 'https://hs.example/mcp', ['64:ff9b::169.254.169.254']],
    ['the local-use NAT64 prefix', 'https://[64:ff9b:1::a9fe:a9fe]/mcp', []],
    ['6to4 of the metadata address', 'https://[2002:a9fe:a9fe::1]/mcp', []],
    ['IPv4-compatible metadata address', 'https://hs.example/mcp', ['::a9fe:a9fe']],
    ['Teredo embedding the metadata address', 'https://hs.example/mcp', ['2001:0:4136:e378:8000:63bf:5601:5601']],
    ['plain http to a LAN host', 'http://10.1.2.3:8888/mcp/', ['10.1.2.3']],
    ['plain http to a public name', 'http://hs.example/mcp/', ['203.0.113.5']],
    ['plain http to a name resolving to loopback AND LAN', 'http://mixed.example/mcp/', ['127.0.0.1', '10.0.0.5']],
  ])('refuses %s', async (_name, url, addresses) => {
    await expect(assertEndpointAllowed(url, resolveTo(addresses))).rejects.toThrow(EgressError)
  })

  it('refuses the embedded forms for gateway base URLs too', async () => {
    await expect(assertGatewayHostAllowed('http://[64:ff9b::a9fe:a9fe]', resolveTo([]))).rejects.toThrow(EgressError)
  })

  it('refuses an unresolvable host', async () => {
    await expect(
      assertEndpointAllowed('https://nx.example/mcp', () => Promise.reject(new Error('ENOTFOUND'))),
    ).rejects.toThrow(/cannot be resolved/)
  })

  it.each([
    ['https to a public host', 'https://hs.example/mcp/', ['203.0.113.5']],
    ['https to a LAN host', 'https://10.1.2.3/mcp/', ['10.1.2.3']],
    ['https to a public NAT64 address', 'https://[64:ff9b::cb00:710a]/mcp/', []],
    ['http to 127.0.0.1', 'http://127.0.0.1:8888/mcp/', []],
    ['http to [::1]', 'http://[::1]:8888/mcp/', []],
    ['http to localhost resolving to loopback', 'http://localhost:8888/mcp/', ['127.0.0.1', '::1']],
  ])('allows %s, returning the checked addresses', async (_name, url, addresses) => {
    const checked = await assertEndpointAllowed(url, resolveTo(addresses))
    expect(checked.length).toBeGreaterThan(0)
  })

  it('reads the IPv4 inside each embedding form', () => {
    expect(ipv6Bytes('::1')).toEqual([...Array<number>(15).fill(0), 1])
    expect(ipv6Bytes('1::2::3')).toBeUndefined()
    expect(embeddedIPv4('64:ff9b::a9fe:a9fe')).toBe('169.254.169.254')
    expect(embeddedIPv4('64:ff9b::169.254.169.254')).toBe('169.254.169.254')
    expect(embeddedIPv4('2002:a9fe:a9fe::')).toBe('169.254.169.254')
    expect(embeddedIPv4('::ffff:a9fe:a9fe')).toBe('169.254.169.254')
    expect(embeddedIPv4('2001:0:4136:e378:8000:63bf:5601:5601')).toBe('169.254.169.254')
    expect(embeddedIPv4('2001:db8::1')).toBeUndefined()
  })
})

describe('tool names as Claude Code sees them', () => {
  it('rewrites every character outside [A-Za-z0-9_-] to _', () => {
    expect(harnessToolName('files.list')).toBe('files_list')
    expect(harnessToolName('my tool')).toBe('my_tool')
    expect(harnessToolName('a-b_C9')).toBe('a-b_C9')
  })

  it('accepts tiers only for names Claude Code does not rewrite', () => {
    expect(validateToolTiers({ recall: 'read', 'a-b_C9': 'write' })).toEqual({ recall: 'read', 'a-b_C9': 'write' })
    expect(() => validateToolTiers({ 'files.list': 'read' })).toThrow(/cannot be given a tier/)
    expect(() => validateToolTiers({ 'my tool': 'read' })).toThrow(PluginError)
    expect(() => validateToolTiers({ recall: 'admin' })).toThrow(/must be one of read, write, outward/)
  })

  it('accepts any name in disabled_tools, dotted or with spaces', () => {
    expect(validateDisabledTools(['files.delete', 'my tool', 'b', 'b'])).toEqual(['b', 'files.delete', 'my tool'])
    expect(() => validateDisabledTools(['a\nb'])).toThrow(PluginError)
    expect(() => validateDisabledTools(['x'.repeat(129)])).toThrow(PluginError)
  })

  it('describes renamed and colliding tools as outward, with their harness names', () => {
    const tools = describeTools(memory, [
      { name: 'recall', annotations: { readOnlyHint: true } },
      { name: 'files.list' },
      { name: 'files_list' },
      { name: 'files.get', annotations: { readOnlyHint: true } },
      { name: 'my tool' },
    ])
    expect(tools.map((t) => [t.name, t.harness_name, t.tier, t.tier_source, t.suggested_tier, t.disabled])).toEqual([
      ['recall', 'mcp__memory__recall', 'read', 'explicit', 'read', false],
      ['files.list', 'mcp__memory__files_list', 'outward', 'collision', null, true],
      ['files_list', 'mcp__memory__files_list', 'outward', 'collision', null, true],
      ['files.get', 'mcp__memory__files_get', 'outward', 'renamed', 'read', false],
      ['my tool', 'mcp__memory__my_tool', 'outward', 'renamed', null, true],
    ])
  })
})

describe('the forwarder tool policy', () => {
  const route = () => ({ plugin: memory, collided: new Set<string>() })

  it('hides disabled tools (by raw or harness name) and every tool of a colliding pair', () => {
    const r = route()
    const kept = filterToolList(r, [
      { name: 'recall' },
      { name: 'files.list' },
      { name: 'files_list' },
      { name: 'files.delete' },
      { name: 'my tool' },
      { name: 'delete_bank' },
      { name: 'retain' },
      { nope: true },
    ])
    expect(kept).toEqual([{ name: 'recall' }, { name: 'retain' }])
    expect([...r.collided]).toEqual(['files_list'])
  })

  it('refuses calls to disabled, colliding, or renamed-into-a-tier tools; lets the rest through', () => {
    const r = route()
    expect(callRefusal(r, 'recall')).toBeUndefined()
    expect(callRefusal(r, 'retain')).toBeUndefined() // outward: the permission seam decides
    expect(callRefusal(r, 'files.get')).toBeUndefined() // renamed, but no tier to borrow
    expect(callRefusal(r, 'delete_bank')).toMatch(/disabled/)
    expect(callRefusal(r, 'files.delete')).toMatch(/disabled/)
    expect(callRefusal(r, 'my tool')).toMatch(/disabled/)
    // files.list would borrow files_list's read tier: refused even before any list.
    expect(callRefusal(r, 'files.list')).toMatch(/not the tool "files_list"/)
    r.collided.add('files_list')
    expect(callRefusal(r, 'files_list')).toMatch(/shares the name/)
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
    expect(tierOf('mcp__memory__toString')).toBe('outward')
    expect(tierOf('mcp__memory__constructor')).toBe('outward')
  })

  it('answers only for its own prefix', () => {
    expect(tierOf('mcp__scadbuddy__render_model')).toBeUndefined()
    expect(tierOf('mcp__memoryx__recall')).toBeUndefined()
    expect(tierOf('Bash')).toBeUndefined()
  })

  it('takes precedence over the run tier resolver for plugin tools only', () => {
    const combined = harnessTierOf({ remotePlugins: [forwardedMemory], tierOf: () => 'read' })
    expect(combined('mcp__memory__retain')).toBe('outward')
    expect(combined('mcp__scadbuddy__render_model')).toBe('read')
    expect(harnessTierOf({ remotePlugins: [forwardedMemory] })('mcp__scadbuddy__x')).toBeUndefined()
  })
})

describe('auth headers', () => {
  it('accepts ordinary header names and refuses transport and hop-by-hop ones', () => {
    expect(validateHeaderName('Authorization')).toBe('Authorization')
    expect(validateHeaderName('X-Api-Key')).toBe('X-Api-Key')
    for (const bad of [
      'Host',
      'content-type',
      'Mcp-Session-Id',
      'Proxy-Authorization',
      'TE',
      'Upgrade',
      'Expect',
      'Keep-Alive',
      'Trailer',
      'User-Agent',
      'Origin',
      'Accept-Encoding',
      'bad header',
      'a:b',
      '',
    ]) {
      expect(() => validateHeaderName(bad)).toThrow(PluginError)
    }
  })

  it('redacts the bare token after an auth scheme as well as the whole value', () => {
    expect(headerSecretVariants(`Bearer ${TOKEN}`)).toEqual([`Bearer ${TOKEN}`, TOKEN])
    expect(headerSecretVariants(`basic ${TOKEN}`)).toEqual([`basic ${TOKEN}`, TOKEN])
    expect(headerSecretVariants(`Token ${TOKEN}`)).toEqual([`Token ${TOKEN}`, TOKEN])
    expect(headerSecretVariants(TOKEN)).toEqual([TOKEN])
  })
})

describe('the SDK options a plugin becomes', () => {
  const paths = { stateDir: '/var/lib/scadbuddy-agent' }
  const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-api03-unit-0000' }

  it('is a Streamable HTTP server at the forwarder URL, with no header', () => {
    const { mcpServers, disallowedTools } = remotePluginOptions(
      [forwardedMemory, { ...forwardedMemory, name: 'other' }],
      new Set(),
    )
    expect(mcpServers).toEqual({
      memory: { type: 'http', url: forwardedMemory.url, alwaysLoad: true },
      other: { type: 'http', url: forwardedMemory.url, alwaysLoad: true },
    })
    // Disabled tools by the name Claude Code gives them.
    expect(disallowedTools).toEqual([
      'mcp__memory__delete_bank',
      'mcp__memory__files_delete',
      'mcp__memory__my_tool',
      'mcp__other__delete_bank',
      'mcp__other__files_delete',
      'mcp__other__my_tool',
    ])
  })

  it('refuses a plugin whose name is already an in-process server, or used twice', () => {
    expect(() => remotePluginOptions([forwardedMemory], new Set(['memory']))).toThrow(PluginConfigError)
    expect(() => remotePluginOptions([forwardedMemory, forwardedMemory], new Set())).toThrow(PluginConfigError)
  })

  it('wires into the harness options and adds nothing to the environment', () => {
    const options = buildHarnessOptions({ paths, credential, prompt: 'hi', remotePlugins: [forwardedMemory] })
    expect(Object.keys(options.mcpServers ?? {})).toEqual(['memory'])
    expect(options.disallowedTools).toContain('mcp__memory__files_delete')
    expect(options.tools).toEqual([])
    expect(Object.keys(options.env ?? {}).filter((k) => k.startsWith('SCADBUDDY_'))).toEqual([])
  })
})
