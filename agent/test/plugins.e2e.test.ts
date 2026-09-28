import { spawn } from 'node:child_process'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { query, type SDKMessage, type SDKResultMessage, type SpawnedProcess } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Credential } from '../src/credentials.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { ToolDecision } from '../src/harness/permissions.js'
import { buildHarnessOptions, type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import type { RemotePlugin } from '../src/plugins/registry.js'
import { testPlugin } from '../src/plugins/testConnection.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'

// #297 end to end: the real SDK and its bundled Claude Code binary, a local
// fake Anthropic endpoint (test/support/fakeAnthropic.ts) and a local
// Streamable HTTP MCP server built on @modelcontextprotocol/sdk
// (test/support/fakeMcp.ts). Nothing leaves the machine.

const GATEWAY_TOKEN = 'gw-plugin-e2e-token-0000111122223333'
const PLUGIN_TOKEN = 'hs-plugin-e2e-token-4444555566667777'

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

describe('plugin connection test (tools/list over Streamable HTTP)', () => {
  let mcp: FakeMcp
  beforeEach(async () => {
    mcp = await startFakeMcp()
  })
  afterEach(async () => {
    await mcp.close()
  })

  it('lists the tools with their effective tiers and annotation suggestions, sending the header', async () => {
    const result = await testPlugin(
      {
        name: 'my-memory',
        url: mcp.url,
        header: { name: 'Authorization', value: `Bearer ${PLUGIN_TOKEN}` },
        toolTiers: { recall: 'read' },
        disabledTools: ['forget'],
      },
      { timeoutMs: 5000 },
    )
    expect(result.ok).toBe(true)
    expect(result.server).toEqual({ name: 'fake-memory', version: '0.0.1' })
    expect(result.tools.map((t) => [t.name, t.harness_name, t.tier, t.tier_source, t.suggested_tier, t.disabled])).toEqual([
      ['recall', 'mcp__my-memory__recall', 'read', 'explicit', 'read', false],
      ['retain', 'mcp__my-memory__retain', 'outward', 'default', null, false],
      ['forget', 'mcp__my-memory__forget', 'outward', 'default', null, true],
    ])
    // No tool was called: the test only lists.
    expect(mcp.calls).toEqual([])
    const posts = mcp.requests.filter((r) => r.method === 'POST')
    expect(posts.length).toBeGreaterThanOrEqual(2) // initialize (+ initialized) + tools/list
    for (const r of posts) expect(r.headers.authorization).toBe(`Bearer ${PLUGIN_TOKEN}`)
    expect(JSON.stringify(result)).not.toContain(PLUGIN_TOKEN)
  })

  it('fails with a clear reason, never echoing the secret, when the endpoint refuses', async () => {
    const result = await testPlugin(
      {
        name: 'my-memory',
        url: mcp.url.replace('/bank-1/', '/nope/'),
        header: { name: 'Authorization', value: `Bearer ${PLUGIN_TOKEN}` },
        toolTiers: {},
        disabledTools: [],
      },
      { timeoutMs: 5000 },
    )
    expect(result.ok).toBe(false)
    expect(result.detail).toMatch(/404/)
    expect(JSON.stringify(result)).not.toContain(PLUGIN_TOKEN)
  })

  it('times out', async () => {
    const result = await testPlugin(
      { name: 'slow', url: 'http://127.0.0.1:9/mcp', toolTiers: {}, disabledTools: [] },
      { timeoutMs: 300, fetch: () => new Promise(() => {}) },
    )
    expect(result).toMatchObject({ ok: false, detail: expect.stringMatching(/timed out/) as unknown })
  })

  it('does not follow a redirect (the egress check applies to the URL it was given)', async () => {
    const result = await testPlugin(
      { name: 'redirect', url: mcp.url, toolTiers: {}, disabledTools: [] },
      {
        timeoutMs: 2000,
        fetch: (url, init) => {
          expect(init?.redirect).toBe('error')
          return fetch(url, init)
        },
      },
    )
    expect(result.ok).toBe(true)
  })
})

describe.skipIf(cliMissing !== undefined)(
  `a harness run with a registered remote MCP plugin${cliMissing ? ` (skipped: ${cliMissing})` : ''}`,
  () => {
    let fake: FakeAnthropic
    let mcp: FakeMcp
    let script: (request: RecordedRequest) => Reply
    let stateDir: string

    beforeEach(async () => {
      stateDir = await mkdtemp(path.join(os.tmpdir(), 'plugins-e2e-'))
      await ensureStateDirs({ stateDir })
      fake = await startFakeAnthropic((r) => script(r))
      mcp = await startFakeMcp()
    })
    afterEach(async () => {
      await fake.close()
      await mcp.close()
    })

    const gateway = (): Credential => ({ kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN })
    const plugin = (): RemotePlugin => ({
      // A hyphenated name, as the registry allows: the SDK keeps it in the tool names.
      name: 'my-memory',
      url: mcp.url,
      header: { name: 'Authorization', value: `Bearer ${PLUGIN_TOKEN}` },
      toolTiers: { recall: 'read' },
      disabledTools: ['forget'],
    })
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

    it('offers the plugin tools namespaced, minus the disabled one, and runs a read-tier tool', async () => {
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
        remotePlugins: [plugin()],
        onDecision: (name, d) => decisions.push([name, d]),
      })
      expect(result).toMatchObject({ subtype: 'success', result: 'Found it.' })
      expect(offered()).toEqual(['mcp__my-memory__recall', 'mcp__my-memory__retain'])
      expect(decisions).toEqual([['mcp__my-memory__recall', { decision: 'allow', tier: 'read' }]])
      expect(mcp.calls).toEqual(['recall:PETG settings'])
      // The tool result reached the model.
      expect(lastContent(fake.messageCalls().at(-1)!)).toContain('remembered: PETG settings')
      // The ${VAR} header reference was expanded by Claude Code: the MCP
      // server got the real value on every request.
      const posts = mcp.requests.filter((r) => r.method === 'POST')
      expect(posts.length).toBeGreaterThan(0)
      for (const r of posts) expect(r.headers.authorization).toBe(`Bearer ${PLUGIN_TOKEN}`)
      // The init message reports the server as connected.
      const init = messages.find((m) => m.type === 'system' && m.subtype === 'init')
      expect(init && 'mcp_servers' in init ? init.mcp_servers : undefined).toEqual([
        { name: 'my-memory', status: 'connected', source: 'dynamic' },
      ])
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
        remotePlugins: [plugin()],
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

    it('never puts the header value on the Claude Code command line', async () => {
      script = () => ({ text: 'ok' })
      const options = buildHarnessOptions({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'hi',
        model: 'claude-sonnet-4-5',
        maxTurns: 1,
        remotePlugins: [plugin()],
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
      const config = argv[argv.indexOf('--mcp-config') + 1] ?? ''
      expect(config).toContain('"my-memory"')
      expect(config).toContain('${SCADBUDDY_PLUGIN_0_HEADER}')
      expect(argv.join(' ')).not.toContain(PLUGIN_TOKEN)
      // ...and the server still received it.
      for (const r of mcp.requests.filter((q) => q.method === 'POST')) {
        expect(r.headers.authorization).toBe(`Bearer ${PLUGIN_TOKEN}`)
      }
    })
  },
)
