import { spawn } from 'node:child_process'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { query, type SDKMessage, type SDKResultMessage, type SpawnedProcess } from '@anthropic-ai/claude-agent-sdk'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Credential } from '../src/credentials.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { ToolDecision } from '../src/harness/permissions.js'
import { buildHarnessOptions, type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { forwardForRun, PluginForwarder, type PluginsForRun } from '../src/plugins/forwarder.js'
import type { RemotePlugin } from '../src/plugins/registry.js'
import { testPlugin } from '../src/plugins/testConnection.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'

// #297 end to end: the real SDK and its bundled Claude Code binary, a local
// fake Anthropic endpoint (test/support/fakeAnthropic.ts), the loopback
// plugin forwarder (src/plugins/forwarder.ts), and local Streamable HTTP MCP
// servers built on @modelcontextprotocol/sdk (test/support/fakeMcp.ts).
// Nothing leaves the machine.

const GATEWAY_TOKEN = 'gw-plugin-e2e-token-0000111122223333'
const PLUGIN_TOKEN = 'hs-plugin-e2e-token-4444555566667777'
// Tools whose names Claude Code rewrites: a dotted one, and a pair that
// collide on one harness name (files.list and files_list → files_list).
const EXTRA_TOOLS = ['files.delete', 'files.list', 'files_list']

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

let forwarder: PluginForwarder
beforeAll(async () => {
  forwarder = await PluginForwarder.start()
})
afterAll(async () => {
  await forwarder.close()
})

const plugin = (url: string, extra: Partial<RemotePlugin> = {}): RemotePlugin => ({
  // A hyphenated name, as the registry allows: the SDK keeps it in the tool names.
  name: 'my-memory',
  url,
  header: { name: 'Authorization', value: `Bearer ${PLUGIN_TOKEN}` },
  toolTiers: { recall: 'read', files_list: 'read' },
  disabledTools: ['forget', 'files.delete'],
  ...extra,
})

/** A second server that must never be reached (a redirect or OAuth target). */
async function trap(): Promise<FakeMcp> {
  return startFakeMcp({ path: '/never' })
}

describe('plugin connection test, through the forwarder', () => {
  let mcp: FakeMcp
  let other: FakeMcp
  beforeEach(async () => {
    other = await trap()
  })
  afterEach(async () => {
    await mcp.close()
    await other.close()
  })

  it('lists every tool with its harness name, tier and collisions, sending the header', async () => {
    mcp = await startFakeMcp({ extraTools: EXTRA_TOOLS })
    const result = await testPlugin(plugin(mcp.url), '127.0.0.1', forwarder, { timeoutMs: 5000 })
    expect(result.ok).toBe(true)
    expect(result.server).toEqual({ name: 'fake-memory', version: '0.0.1' })
    expect(
      result.tools.map((t) => [t.name, t.harness_name, t.tier, t.tier_source, t.suggested_tier, t.disabled]),
    ).toEqual([
      ['files.delete', 'mcp__my-memory__files_delete', 'outward', 'renamed', null, true],
      ['files.list', 'mcp__my-memory__files_list', 'outward', 'collision', null, true],
      ['files_list', 'mcp__my-memory__files_list', 'outward', 'collision', null, true],
      ['recall', 'mcp__my-memory__recall', 'read', 'explicit', 'read', false],
      ['retain', 'mcp__my-memory__retain', 'outward', 'default', null, false],
      ['forget', 'mcp__my-memory__forget', 'outward', 'default', null, true],
    ])
    expect(result.tools.find((t) => t.name === 'files.list')?.collides_with).toEqual(['files_list'])
    expect(mcp.calls).toEqual([])
    const posts = mcp.requests.filter((r) => r.method === 'POST')
    expect(posts.length).toBeGreaterThanOrEqual(2)
    for (const r of posts) expect(r.headers.authorization).toBe(`Bearer ${PLUGIN_TOKEN}`)
    expect(JSON.stringify(result)).not.toContain(PLUGIN_TOKEN)
    expect(forwarder.size).toBe(0) // released
  })

  it('connects to the checked address, not to whatever the name resolves to', async () => {
    mcp = await startFakeMcp()
    const port = new URL(mcp.url).port
    // .invalid never resolves (RFC 6761): only the pinned address can reach it.
    const url = `http://plugin.invalid:${port}/mcp/bank-1/`
    const result = await testPlugin(plugin(url), '127.0.0.1', forwarder, { timeoutMs: 5000 })
    expect(result.ok).toBe(true)
    expect(mcp.requests[0]?.headers.host).toBe(`plugin.invalid:${port}`)
  })

  it('fails with the status, never echoing the secret, when the endpoint refuses', async () => {
    mcp = await startFakeMcp()
    const result = await testPlugin(plugin(mcp.url.replace('/bank-1/', '/nope/')), '127.0.0.1', forwarder, {
      timeoutMs: 5000,
    })
    expect(result.ok).toBe(false)
    expect(result.detail).toMatch(/404/)
    expect(JSON.stringify(result)).not.toContain(PLUGIN_TOKEN)
  })

  it('times out', async () => {
    mcp = await startFakeMcp()
    const result = await testPlugin(plugin(mcp.url), '127.0.0.1', forwarder, {
      timeoutMs: 300,
      fetch: () => new Promise(() => {}),
    })
    expect(result).toMatchObject({ ok: false, detail: expect.stringMatching(/timed out/) as unknown })
  })

  it('does not follow a redirect: a 307 to another origin fails, and that origin is never contacted', async () => {
    mcp = await startFakeMcp({
      intercept: (_req, res) => {
        res.writeHead(307, { location: `${other.url.replace('/never', '')}/mcp/bank-1/` }).end()
        return true
      },
    })
    const result = await testPlugin(plugin(mcp.url), '127.0.0.1', forwarder, { timeoutMs: 5000 })
    expect(result.ok).toBe(false)
    expect(result.detail).toMatch(/redirect/)
    expect(mcp.requests.length).toBeGreaterThan(0)
    expect(other.requests).toEqual([])
  })

  it('does not start OAuth discovery: a 401 with resource_metadata fails, and the metadata URL is never fetched', async () => {
    mcp = await startFakeMcp({
      intercept: (_req, res) => {
        res
          .writeHead(401, {
            'www-authenticate': `Bearer resource_metadata="${other.url.replace('/never', '')}/.well-known/oauth-protected-resource"`,
          })
          .end()
        return true
      },
    })
    const result = await testPlugin(plugin(mcp.url), '127.0.0.1', forwarder, { timeoutMs: 5000 })
    expect(result.ok).toBe(false)
    expect(result.detail).toMatch(/401/)
    expect(other.requests).toEqual([])
    expect(JSON.stringify(result)).not.toContain(PLUGIN_TOKEN)
  })
})

describe.skipIf(cliMissing !== undefined)(
  `a harness run with a registered remote MCP plugin${cliMissing ? ` (skipped: ${cliMissing})` : ''}`,
  () => {
    let fake: FakeAnthropic
    let mcp: FakeMcp
    let other: FakeMcp
    let script: (request: RecordedRequest) => Reply
    let stateDir: string
    let forwarded: PluginsForRun | undefined

    beforeEach(async () => {
      stateDir = await mkdtemp(path.join(os.tmpdir(), 'plugins-e2e-'))
      await ensureStateDirs({ stateDir })
      fake = await startFakeAnthropic((r) => script(r))
      other = await trap()
      mcp = await startFakeMcp({ extraTools: EXTRA_TOOLS })
    })
    afterEach(async () => {
      forwarded?.release()
      forwarded = undefined
      await fake.close()
      await mcp.close()
      await other.close()
    })

    const gateway = (): Credential => ({ kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN })
    const forward = (p: RemotePlugin) => {
      forwarded = forwardForRun({ plugins: [{ plugin: p, address: '127.0.0.1' }], problems: [] }, forwarder)
      return forwarded.plugins
    }
    const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
    const offered = () =>
      [...new Set(fake.messageCalls().flatMap((c) => (c.body?.tools ?? []).map((t) => t.name)))].sort()

    async function collect(run: HarnessRun): Promise<{ messages: SDKMessage[]; result: SDKResultMessage; stderr: string[] }> {
      const messages: SDKMessage[] = []
      const stderr: string[] = []
      try {
        for await (const m of runHarness({ ...run, stderr: (l) => stderr.push(l) })) messages.push(m)
      } catch (err) {
        if (!messages.some((m) => m.type === 'result')) throw err
      }
      const result = messages.find((m): m is SDKResultMessage => m.type === 'result')
      if (!result) throw new Error(`no result message; stderr: ${stderr.join('')}`)
      return { messages, result, stderr }
    }
    const initOf = (messages: SDKMessage[]) => {
      const init = messages.find((m) => m.type === 'system' && m.subtype === 'init')
      return init && 'mcp_servers' in init ? init.mcp_servers : undefined
    }

    it('offers the plugin tools namespaced, minus disabled and colliding ones, and runs a read-tier tool', async () => {
      script = (r) =>
        lastContent(r).includes('tool_result')
          ? { text: 'Found it.' }
          : { toolUse: { name: 'mcp__my-memory__recall', input: { query: 'PETG settings' } } }
      const decisions: [string, ToolDecision][] = []
      const { result, messages, stderr } = await collect({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'What do you remember about PETG?',
        model: 'claude-sonnet-4-5',
        remotePlugins: forward(plugin(mcp.url)),
        onDecision: (name, d) => decisions.push([name, d]),
      })
      expect(result).toMatchObject({ subtype: 'success', result: 'Found it.' })
      // forget and files.delete are disabled; files.list/files_list collide, so both are hidden.
      expect(offered()).toEqual(['mcp__my-memory__recall', 'mcp__my-memory__retain'])
      expect(decisions).toEqual([['mcp__my-memory__recall', { decision: 'allow', tier: 'read' }]])
      expect(mcp.calls).toEqual(['recall:PETG settings'])
      expect(lastContent(fake.messageCalls().at(-1)!)).toContain('remembered: PETG settings')
      // #258: the plugin's result reached the model inside the untrusted-data envelope, under its harness name.
      expect(lastContent(fake.messageCalls().at(-1)!)).toContain('untrusted_data')
      expect(lastContent(fake.messageCalls().at(-1)!)).toContain('mcp__my-memory__recall')
      // The forwarder added the header on every request to the plugin.
      const posts = mcp.requests.filter((r) => r.method === 'POST')
      expect(posts.length).toBeGreaterThan(0)
      for (const r of posts) expect(r.headers.authorization).toBe(`Bearer ${PLUGIN_TOKEN}`)
      expect(initOf(messages)).toEqual([{ name: 'my-memory', status: 'connected', source: 'dynamic' }])
      expect(JSON.stringify(messages)).not.toContain(PLUGIN_TOKEN)
      expect(stderr.join('\n')).not.toContain(PLUGIN_TOKEN)
    })

    it('denies an outward (unlisted) plugin tool as needing approval; it never reaches the server', async () => {
      script = (r) =>
        lastContent(r).includes('tool_result')
          ? { text: 'I need approval to store that.' }
          : { toolUse: { name: 'mcp__my-memory__retain', input: { text: 'user likes PETG' } } }
      const decisions: [string, ToolDecision][] = []
      const { result } = await collect({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'Remember that I like PETG',
        model: 'claude-sonnet-4-5',
        remotePlugins: forward(plugin(mcp.url)),
        onDecision: (name, d) => decisions.push([name, d]),
      })
      expect(result.subtype).toBe('success')
      expect(decisions.map(([n, d]) => [n, d.decision, d.tier])).toEqual([
        ['mcp__my-memory__retain', 'needs_approval', 'outward'],
      ])
      expect(result.permission_denials.map((d) => d.tool_name)).toEqual(['mcp__my-memory__retain'])
      expect(mcp.calls).toEqual([])
      expect(lastContent(fake.messageCalls().at(-1)!)).toMatch(/needs a human approval in the ScadBuddy UI/)
    })

    it('cannot run a colliding tool through its read-tier harness name', async () => {
      // files_list is tiered read, but files.list shares its harness name, so both are hidden.
      script = (r) =>
        lastContent(r).includes('tool_result')
          ? { text: 'No such tool.' }
          : { toolUse: { name: 'mcp__my-memory__files_list', input: { text: 'x' } } }
      const { result } = await collect({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'List files',
        model: 'claude-sonnet-4-5',
        remotePlugins: forward(plugin(mcp.url)),
      })
      expect(result.subtype).toBe('success')
      expect(mcp.calls).toEqual([])
      expect(lastContent(fake.messageCalls().at(-1)!)).toMatch(/"is_error":true/)
    })

    it('a plugin endpoint that redirects is not connected, and the redirect target is never contacted', async () => {
      const redirecting = await startFakeMcp({
        intercept: (_req, res) => {
          res.writeHead(307, { location: `${other.url.replace('/never', '')}/mcp/bank-1/` }).end()
          return true
        },
      })
      try {
        script = () => ({ text: 'ok' })
        const { messages } = await collect({
          paths: { stateDir },
          credential: gateway(),
          prompt: 'hi',
          model: 'claude-sonnet-4-5',
          maxTurns: 1,
          remotePlugins: forward(plugin(redirecting.url)),
        })
        expect(initOf(messages)?.find((s) => s.name === 'my-memory')?.status).not.toBe('connected')
        expect(redirecting.requests.length).toBeGreaterThan(0)
        expect(other.requests).toEqual([])
      } finally {
        await redirecting.close()
      }
    })

    it('never puts the plugin endpoint or its header on the Claude Code command line', async () => {
      script = () => ({ text: 'ok' })
      const options = buildHarnessOptions({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'hi',
        model: 'claude-sonnet-4-5',
        maxTurns: 1,
        remotePlugins: forward(plugin(mcp.url)),
      })
      let argv: string[] = []
      options.spawnClaudeCodeProcess = (spawnOptions) => {
        argv = spawnOptions.args
        const child = spawn(spawnOptions.command, spawnOptions.args, {
          cwd: spawnOptions.cwd,
          env: spawnOptions.env,
          stdio: ['pipe', 'pipe', 'pipe'],
        })
        return child as unknown as SpawnedProcess
      }
      const messages: SDKMessage[] = []
      for await (const m of query({ prompt: 'hi', options })) messages.push(m)
      expect(messages.some((m) => m.type === 'result')).toBe(true)
      const config = JSON.parse(argv[argv.indexOf('--mcp-config') + 1] ?? '{}') as {
        mcpServers: Record<string, { url: string; headers?: unknown }>
      }
      expect(config.mcpServers['my-memory']?.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/p\/[A-Za-z0-9_-]{24}$/)
      expect(config.mcpServers['my-memory']?.headers).toBeUndefined()
      expect(argv.join(' ')).not.toContain(PLUGIN_TOKEN)
      expect(argv.join(' ')).not.toContain(mcp.url)
      expect(Object.values(options.env ?? {}).join(' ')).not.toContain(PLUGIN_TOKEN)
    })
  },
)
