import { describe, expect, it } from 'vitest'
import {
  authenticate,
  DEFAULT_ANONYMOUS_NETWORKS,
  DEFAULT_MCP_AUTH,
  type McpAuthSettings,
  mcpAuthSettings,
  SETTING_MCP_ANONYMOUS_NETWORKS,
  SETTING_MCP_AUTH_MODE,
} from '../src/auth/authenticate.js'
import { InMemoryTokenStore } from './support/memoryTokens.js'

// `disabled` mode serves `anonymous` only to a caller whose address is in
// `ai_settings.mcp_anonymous_networks` (#7767). Any other caller is handled as
// `bearer` mode handles it.

const DISABLED: McpAuthSettings = { ...DEFAULT_MCP_AUTH, mode: 'disabled', configuredMode: 'disabled' }
const MCP = 'https://scadbuddy.test/mcp'

function call(callerAddress: string | undefined, settings: Partial<McpAuthSettings> = {}) {
  const tokens = new InMemoryTokenStore()
  return {
    tokens,
    run: (token?: string) =>
      authenticate(new Request(MCP, { method: 'POST', headers: token ? { authorization: `Bearer ${token}` } : {} }), {
        settings: { ...DISABLED, ...settings },
        tokens,
        clientAddress: '10.0.0.7',
        callerAddress,
      }),
  }
}

describe('authenticate in disabled mode: the anonymous networks', () => {
  it('serves a caller from a private or loopback network as anonymous', async () => {
    const addresses = ['127.0.0.1', '::1', '10.1.2.3', '172.16.0.1', '172.31.255.255', '192.168.1.50', 'fd12::1', '::ffff:192.168.1.50']
    for (const address of addresses) {
      const result = await call(address).run()
      expect(result.ok, address).toBe(true)
      if (result.ok) expect(result.principal).toMatchObject({ id: 'anonymous', kind: 'anonymous', clientIp: '10.0.0.7' })
    }
  })

  it('refuses a caller from a public address with the bearer challenge', async () => {
    for (const address of ['203.0.113.9', '172.32.0.1', '2001:db8::1', '::ffff:203.0.113.9']) {
      const result = await call(address).run()
      expect(result.ok, address).toBe(false)
      if (!result.ok) {
        expect(result.response.status).toBe(401)
        expect(result.response.headers.get('www-authenticate')).toBe('Bearer realm="scadbuddy"')
        expect(await result.response.json()).toEqual({ error: 'a bearer token is required' })
      }
    }
  })

  it('serves a public caller with a valid token as that token, and refuses a bad one', async () => {
    const { tokens, run } = call('203.0.113.9')
    const { token, record } = await tokens.mint({ name: 'ci', tier: 'read' })
    const result = await run(token)
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.principal).toMatchObject({ id: `token:${record.id}`, kind: 'bearer', tiers: ['read'], clientIp: '10.0.0.7' })
    }
    const bad = await run('sbmcp_unknown')
    expect(bad.ok).toBe(false)
    if (!bad.ok) expect(bad.response.headers.get('www-authenticate')).toBe('Bearer realm="scadbuddy", error="invalid_token"')
  })

  it('refuses a caller whose address is unknown', async () => {
    const result = await call(undefined).run()
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.response.status).toBe(401)
  })

  it('uses the configured list, and an empty list serves no anonymous caller', async () => {
    const custom = { anonymousNetworks: ['198.51.100.0/24', '2001:db8:1::/48'] }
    expect((await call('198.51.100.7', custom).run()).ok).toBe(true)
    expect((await call('2001:db8:1::5', custom).run()).ok).toBe(true)
    expect((await call('::ffff:198.51.100.7', custom).run()).ok).toBe(true)
    expect((await call('192.168.1.50', custom).run()).ok).toBe(false)
    expect((await call('127.0.0.1', { anonymousNetworks: [] }).run()).ok).toBe(false)
  })
})

function reader(values: Record<string, unknown>) {
  return { get: <T>(key: string) => Promise.resolve(values[key] as T | undefined) }
}

describe('mcpAuthSettings: mcp_anonymous_networks', () => {
  it('is loopback, RFC 1918 and IPv6 ULA when unset', async () => {
    expect(DEFAULT_ANONYMOUS_NETWORKS).toEqual([
      '127.0.0.0/8',
      '::1/128',
      '10.0.0.0/8',
      '172.16.0.0/12',
      '192.168.0.0/16',
      'fc00::/7',
    ])
    expect((await mcpAuthSettings(reader({}), () => {})()).anonymousNetworks).toEqual(DEFAULT_ANONYMOUS_NETWORKS)
  })

  it('reads a stored list', async () => {
    const values = { [SETTING_MCP_ANONYMOUS_NETWORKS]: ['192.168.10.0/24', '10.9.9.9'] }
    expect((await mcpAuthSettings(reader(values), () => {})()).anonymousNetworks).toEqual(['192.168.10.0/24', '10.9.9.9'])
  })

  it('fails closed on a stored value that is not a list of networks: an empty list, warned once', async () => {
    for (const bad of ['10.0.0.0/8', ['10.0.0.0/8', 'lan'], ['10.0.0.0/33'], [7], null]) {
      const warned: string[] = []
      const settings = mcpAuthSettings(
        reader({ [SETTING_MCP_AUTH_MODE]: 'bearer', [SETTING_MCP_ANONYMOUS_NETWORKS]: bad }),
        (m) => warned.push(m),
      )
      expect((await settings()).anonymousNetworks, JSON.stringify(bad)).toEqual([])
      await settings()
      expect(warned).toEqual([expect.stringContaining(`${SETTING_MCP_ANONYMOUS_NETWORKS} is ${JSON.stringify(bad)}`)])
    }
  })

  it('names the networks in the disabled-mode warning', async () => {
    const warned: string[] = []
    await mcpAuthSettings(
      reader({ [SETTING_MCP_AUTH_MODE]: 'disabled', [SETTING_MCP_ANONYMOUS_NETWORKS]: ['192.168.10.0/24'] }),
      (m) => warned.push(m),
    )()
    expect(warned).toEqual([expect.stringMatching(/MCP auth is DISABLED.*from 192\.168\.10\.0\/24.*bearer token/s)])
  })
})
