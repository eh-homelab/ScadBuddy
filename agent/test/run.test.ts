import { type ChildProcess, spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, type SDKMessage, type SDKResultMessage, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { Credential } from '../src/credentials.js'
import { credentialGateway, startCredentialGateway, stopCredentialGateway } from '../src/gateway/server.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { RiskTier, ToolDecision } from '../src/harness/permissions.js'
import { PluginRefusedError } from '../src/harness/plugins.js'
import { OWN_PLUGIN_DIR } from '../src/harness/ownPlugin.js'
import { loadAgentDefinitions } from '../src/harness/agentDefinitions.js'
import {
  buildHarnessOptions,
  credentialEnv,
  DEFAULT_MAX_TURNS,
  type HarnessRun,
  harnessTierOf,
  runHarness,
} from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { testConnection } from '../src/harness/testConnection.js'
import { createMemoryHooks, HindsightClient } from '../src/memory/hindsight.js'
import { UNTRUSTED_KEY } from '../src/safety/untrusted.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { startFakeHindsight } from './support/fakeHindsight.js'

const API_KEY = 'sk-ant-api03-unit-test-key-000011112222'
const GATEWAY_TOKEN = 'gw-unit-test-token-3333444455556666'

const OAUTH_TOKEN = 'sk-ant-oat01-test-token-0000'

describe('buildHarnessOptions', () => {
  const paths = { stateDir: '/var/lib/scadbuddy-agent' }
  const base: HarnessRun = { paths, credential: { kind: 'anthropic_api_key', secret: API_KEY }, prompt: 'hi' }

  const lease = { baseUrl: 'http://127.0.0.1:1234/t/tok', token: 'tok' }

  it('passes an API key kind as the gateway lease, its token in ANTHROPIC_API_KEY', () => {
    expect(credentialEnv({ kind: 'anthropic_api_key', secret: API_KEY }, lease)).toEqual({
      ANTHROPIC_BASE_URL: lease.baseUrl,
      ANTHROPIC_API_KEY: 'tok',
    })
  })

  it('passes a Claude Code OAuth token kind as the gateway lease, its token in CLAUDE_CODE_OAUTH_TOKEN', () => {
    // In x-api-key (ANTHROPIC_API_KEY) Anthropic answers an sk-ant-oat01- token with 401.
    expect(credentialEnv({ kind: 'claude_oauth_token', secret: OAUTH_TOKEN }, lease)).toEqual({
      ANTHROPIC_BASE_URL: lease.baseUrl,
      CLAUDE_CODE_OAUTH_TOKEN: 'tok',
    })
  })

  it("passes a gateway kind as the lease, never its own base URL, its token in ANTHROPIC_AUTH_TOKEN", () => {
    expect(credentialEnv({ kind: 'gateway', baseUrl: 'https://llm.example', secret: GATEWAY_TOKEN }, lease)).toEqual({
      ANTHROPIC_BASE_URL: lease.baseUrl,
      ANTHROPIC_AUTH_TOKEN: 'tok',
    })
  })

  it('keeps the least-privilege base, and gives Claude Code a gateway token, never the credential (Step 9.4)', () => {
    const options = buildHarnessOptions(base)
    expect(options.tools).toEqual([])
    expect(options.settingSources).toEqual([])
    expect(options.strictMcpConfig).toBe(true)
    expect(options.permissionMode).toBe('default')
    expect(options.allowedTools).toBeUndefined()
    expect(options.env?.ANTHROPIC_BASE_URL).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/t\/[A-Za-z0-9_-]{32}$/)
    expect(options.env?.ANTHROPIC_BASE_URL?.endsWith(`/t/${options.env.ANTHROPIC_API_KEY}`)).toBe(true)
    expect(JSON.stringify(options)).not.toContain(API_KEY)
    expect(Object.keys(options.env ?? {}).sort()).toEqual(
      [
        'ANTHROPIC_API_KEY',
        'ANTHROPIC_BASE_URL',
        'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
        'CLAUDE_CONFIG_DIR',
        'HOME',
        'TZ',
        ...(process.env.PATH === undefined ? [] : ['PATH']),
      ].sort(),
    )
    expect(process.env.ANTHROPIC_API_KEY).not.toBe(API_KEY)
  })

  it('sets the limits, with defaults', () => {
    expect(buildHarnessOptions(base)).toMatchObject({ maxTurns: DEFAULT_MAX_TURNS, maxBudgetUsd: 1 })
    expect(buildHarnessOptions({ ...base, maxTurns: 3, maxBudgetUsd: 0.2 })).toMatchObject({
      maxTurns: 3,
      maxBudgetUsd: 0.2,
    })
  })

  it('bounds Claude Code’s retries only when asked (#1093: there is a credential to fall back to)', () => {
    expect(buildHarnessOptions(base).env?.CLAUDE_CODE_MAX_RETRIES).toBeUndefined()
    expect(buildHarnessOptions({ ...base, maxRetries: 2 }).env?.CLAUDE_CODE_MAX_RETRIES).toBe('2')
  })

  it('links the abort signal', () => {
    const stop = new AbortController()
    const options = buildHarnessOptions({ ...base, signal: stop.signal })
    expect(options.abortController?.signal.aborted).toBe(false)
    stop.abort()
    expect(options.abortController?.signal.aborted).toBe(true)
  })

  it('loads plugins by absolute local path, and wires both permission seams', () => {
    const options = buildHarnessOptions({ ...base, pluginPaths: ['../plugins/scadbuddy'] })
    expect(options.plugins).toEqual([{ type: 'local', path: path.resolve('../plugins/scadbuddy') }])
    expect(typeof options.canUseTool).toBe('function')
    // The permission seam, then WebFetch's egress check (webFetchEgress.ts).
    expect(options.hooks?.PreToolUse).toHaveLength(2)
    expect(options.hooks?.PreToolUse?.[1]?.matcher).toBe('WebFetch')
  })

  it('adds the trace hooks after the permission seam (#988)', () => {
    const traceHooks = { PreToolUse: [{ hooks: [() => Promise.resolve({})] }], PostToolUse: [{ hooks: [() => Promise.resolve({})] }] }
    const options = buildHarnessOptions({ ...base, traceHooks })
    expect(options.hooks?.PreToolUse).toHaveLength(3)
    expect(options.hooks?.PreToolUse?.[2]).toBe(traceHooks.PreToolUse[0])
    expect(options.hooks?.PostToolUse).toEqual(traceHooks.PostToolUse)
  })

  it("loads ScadBuddy's own plugin first, with Skill and Agent at read and no other built-in (#896)", () => {
    const options = buildHarnessOptions({ ...base, ownPlugin: OWN_PLUGIN_DIR, pluginPaths: ['../plugins/scadbuddy'] })
    expect(options.plugins).toEqual([
      { type: 'local', path: OWN_PLUGIN_DIR },
      { type: 'local', path: path.resolve('../plugins/scadbuddy') },
    ])
    expect(options.tools).toEqual(['Skill', 'Agent'])
    expect(buildHarnessOptions(base).tools).toEqual([])
    const withPlugin = harnessTierOf({ ownPlugin: OWN_PLUGIN_DIR })
    expect(['Skill', 'Agent', 'Task'].map((t) => withPlugin(t))).toEqual(['read', 'read', 'read'])
    expect(withPlugin('Bash')).toBeUndefined()
    expect(harnessTierOf({})('Skill')).toBeUndefined()
  })

  it("offers Skill and Agent, at read, for a package's skills and agents with the own plugin off (#2320)", () => {
    const options = buildHarnessOptions({ ...base, pluginPaths: ['../plugins/scadbuddy'] })
    expect(options.tools).toEqual(['Skill', 'Agent'])
    const tiers = harnessTierOf({ pluginPaths: ['../plugins/scadbuddy'] })
    expect(['Skill', 'Agent', 'Task'].map((t) => tiers(t))).toEqual(['read', 'read', 'read'])
    expect(harnessTierOf({ allowedPluginPaths: ['/x'] })('Skill')).toBe('read')
  })

  it('passes the session options through (#300)', () => {
    const store = { append: () => Promise.resolve(), load: () => Promise.resolve(null) }
    const options = buildHarnessOptions({
      ...base,
      sessionId: '0f8fad5b-d9cb-469f-a165-70867728950e',
      sessionStore: store,
      cwd: '/var/lib/scadbuddy-agent/work/sessions/0f8fad5b-d9cb-469f-a165-70867728950e',
      includePartialMessages: true,
    })
    expect(options).toMatchObject({
      sessionId: '0f8fad5b-d9cb-469f-a165-70867728950e',
      cwd: '/var/lib/scadbuddy-agent/work/sessions/0f8fad5b-d9cb-469f-a165-70867728950e',
      includePartialMessages: true,
    })
    expect(options.sessionStore).toBe(store)
    // Every frame is mirrored as it is written, so a turn cut off by a restart keeps what it had.
    expect(options.sessionStoreFlush).toBe('eager')
    expect(options.resume).toBeUndefined()
    // Without them, the service-wide scratch dir and no mirror, as before.
    const plain = buildHarnessOptions(base)
    expect(plain.cwd).toBe('/var/lib/scadbuddy-agent/work')
    expect(plain.sessionStore).toBeUndefined()
    expect(plain.sessionStoreFlush).toBeUndefined()
    expect(plain.includePartialMessages).toBeUndefined()
  })

  it('refuses a plugin that would start a process with the credential env (spec §8.6)', () => {
    expect(() =>
      buildHarnessOptions({ ...base, pluginPaths: ['../plugins/scadbuddy', 'test/fixtures/plugins/command-hook'] }),
    ).toThrow(PluginRefusedError)
  })

  it('loads a plugin package an admin allowed as it is, and lets its MCP servers start', () => {
    const allowed = path.resolve('test/fixtures/plugins/command-hook')
    const options = buildHarnessOptions({ ...base, allowedPluginPaths: [allowed] })
    expect(options.plugins).toEqual([{ type: 'local', path: allowed }])
    expect(options.strictMcpConfig).toBe(false)
    // The built-ins it names are offered, and skill shell injection runs.
    const withTools = buildHarnessOptions({ ...base, ownPlugin: OWN_PLUGIN_DIR, allowedPluginPaths: [allowed], builtinTools: ['Read', 'Bash', 'Skill'] })
    expect(withTools.tools).toEqual(['Skill', 'Agent', 'Read', 'Bash'])
    expect(withTools.settings).toMatchObject({ disableSkillShellExecution: false })
    // Without an allowed package, neither.
    const without = buildHarnessOptions({ ...base, builtinTools: ['Read'] })
    expect(without.tools).toEqual([])
    expect(without.settings).toMatchObject({ disableSkillShellExecution: true })
    // The vetted list is still checked beside it.
    expect(() =>
      buildHarnessOptions({ ...base, allowedPluginPaths: [allowed], pluginPaths: ['test/fixtures/plugins/stdio-mcp'] }),
    ).toThrow(PluginRefusedError)
  })

  it('redacts the credential from stderr', () => {
    const lines: string[] = []
    const options = buildHarnessOptions({ ...base, stderr: (l) => lines.push(l) })
    options.stderr?.(`request failed with key ${API_KEY}\n`)
    expect(lines).toEqual(['request failed with key [redacted]\n'])
  })

  it('redacts a credential split across stderr chunks', () => {
    const lines: string[] = []
    const options = buildHarnessOptions({ ...base, stderr: (l) => lines.push(l) })
    const cut = 12
    options.stderr?.(`401 with key ${API_KEY.slice(0, cut)}`)
    options.stderr?.(`${API_KEY.slice(cut)} rejected\nnext line\n`)
    expect(lines).toEqual([`401 with key [redacted] rejected\n`, 'next line\n'])
    expect(lines.join('')).not.toContain(API_KEY.slice(0, cut))
  })
})

// End to end through the real SDK and its bundled Claude Code binary, pointed
// at a local fake Anthropic endpoint as a gateway. Nothing here reaches
// Anthropic. The only reason to skip is a platform the SDK ships no binary for.
let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

describe.skipIf(cliMissing !== undefined)(`the harness against a fake Anthropic endpoint${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let stateDir: string
  let handled: string[]

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'harness-'))
    await ensureStateDirs({ stateDir })
    fake = await startFakeAnthropic((r) => script(r))
    handled = []
  })
  afterEach(async () => {
    await fake.close()
  })

  const gateway = (): Credential => ({ kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN })

  /** A stub in-process MCP server standing in for #251's registry. */
  function stubServer() {
    const echo = tool('echo', 'Echo text back', { text: z.string() }, (args) => {
      handled.push(`echo:${args.text}`)
      return Promise.resolve({ content: [{ type: 'text' as const, text: `echo:${args.text}` }] })
    })
    const print = tool('print', 'Send to the printer', { job: z.string() }, (args) => {
      handled.push(`print:${args.job}`)
      return Promise.resolve({ content: [{ type: 'text' as const, text: 'printing' }] })
    })
    return createSdkMcpServer({ name: 'stub', tools: [echo, print] })
  }
  const tiers: Record<string, RiskTier> = { mcp__stub__echo: 'read', mcp__stub__print: 'outward' }

  /** The last message's content as JSON, to see whether a tool result came back. */
  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  /**
   * Runs to the end. For an error result (max turns, budget) the SDK yields the
   * result message and then throws "Claude Code returned an error result"; the
   * result is what the tests assert on, so that throw is swallowed here.
   */
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

  it('runs a scripted turn end to end, sending the gateway token as a bearer token only', async () => {
    script = () => ({ text: 'Hello from the fake.' })
    const { messages, result, stderr } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Say hello',
      model: 'claude-sonnet-4-5',
      maxTurns: 1,
    })
    expect(result).toMatchObject({ subtype: 'success', is_error: false, result: 'Hello from the fake.' })

    const calls = fake.messageCalls()
    expect(calls.length).toBeGreaterThanOrEqual(1)
    for (const call of calls) {
      expect(call.headers.authorization).toBe(`Bearer ${GATEWAY_TOKEN}`)
      expect(call.headers['x-api-key']).toBeUndefined()
    }
    // tools: [] — the model is offered no built-in tool at all.
    expect(calls.flatMap((c) => c.body?.tools ?? [])).toEqual([])
    const init = messages.find((m) => m.type === 'system' && m.subtype === 'init')
    expect(init && 'tools' in init ? init.tools : undefined).toEqual([])
    // The mode in which canUseTool decides every call (test/permissionMode.test.ts).
    expect(init).toMatchObject({ permissionMode: 'default' })
    // The credential appears in no message and no stderr line.
    expect(JSON.stringify(messages)).not.toContain(GATEWAY_TOKEN)
    expect(stderr.join('\n')).not.toContain(GATEWAY_TOKEN)
  })

  describe('through the credential gateway (Step 9.4)', () => {
    beforeEach(async () => {
      // The API-key kinds' upstream is api.anthropic.com; here, the fake.
      await stopCredentialGateway()
      await startCredentialGateway({ anthropicUrl: fake.url })
    })
    afterEach(async () => {
      await stopCredentialGateway()
      await startCredentialGateway()
    })

    const kinds: { name: string; credential: () => Credential; header: (r: RecordedRequest) => unknown; expected: string }[] = [
      { name: 'anthropic_api_key', credential: () => ({ kind: 'anthropic_api_key', secret: API_KEY }), header: (r) => r.headers['x-api-key'], expected: API_KEY },
      {
        name: 'claude_oauth_token',
        credential: () => ({ kind: 'claude_oauth_token', secret: OAUTH_TOKEN }),
        header: (r) => r.headers.authorization,
        expected: `Bearer ${OAUTH_TOKEN}`,
      },
      { name: 'gateway', credential: gateway, header: (r) => r.headers.authorization, expected: `Bearer ${GATEWAY_TOKEN}` },
    ]

    it.each(kinds)('runs a turn on $name with the credential only upstream, never in the process', async ({ credential, header, expected }) => {
      script = () => ({ text: 'Hello through the gateway.' })
      const run = credential()
      let child: ChildProcess | undefined
      let environ: string | undefined
      const given: (Record<string, string | undefined> | undefined)[] = []
      const messages: SDKMessage[] = []
      const q = runHarness({
        paths: { stateDir },
        credential: run,
        prompt: 'Say hello',
        model: 'claude-sonnet-4-5',
        maxTurns: 1,
        spawnClaudeCodeProcess: (o) => {
          given.push(o.env)
          const spawned = spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'inherit'], signal: o.signal })
          child = spawned
          return spawned
        },
      })
      for await (const m of q) {
        // The live process's own environment, as the kernel holds it.
        if (environ === undefined && child?.pid !== undefined) environ = await readFile(`/proc/${child.pid}/environ`, 'utf8').catch(() => '')
        messages.push(m)
      }
      expect(messages.find((m) => m.type === 'result')).toMatchObject({ subtype: 'success', result: 'Hello through the gateway.' })
      expect(given).toHaveLength(1)
      const env = given[0] ?? {}
      expect(env.ANTHROPIC_BASE_URL).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/t\//)
      expect(JSON.stringify(env)).not.toContain(run.secret)
      if (process.platform === 'linux') {
        expect(environ).toContain('ANTHROPIC_BASE_URL=http://127.0.0.1:')
        expect(environ).not.toContain(run.secret)
      }
      // The endpoint got the real credential, in its kind's own header, and never the token.
      const calls = fake.messageCalls()
      expect(calls.length).toBeGreaterThanOrEqual(1)
      const token = env.ANTHROPIC_BASE_URL?.split('/t/')[1] ?? ''
      for (const call of calls) {
        expect(header(call)).toBe(expected)
        expect(JSON.stringify(call.headers)).not.toContain(token)
        // Claude Code still presents an OAuth token as one, which the endpoint needs (measured on 2.1.310).
        expect(String(call.headers['anthropic-beta']).split(',').includes('oauth-2025-04-20')).toBe(run.kind === 'claude_oauth_token')
      }
      // The token died with the query.
      expect(credentialGateway().leases).toBe(0)
    })
  })

  it("runs the turn as a plugin's subagent when given one (a Workflow agent's agentType, #2393)", async () => {
    script = () => ({ text: 'ok' })
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Make a box',
      model: 'claude-opus-5-5',
      maxTurns: 1,
      effort: 'low',
      ownPlugin: OWN_PLUGIN_DIR,
      agent: { name: 'model-author', definition: (await loadAgentDefinitions())['model-author']! },
    })
    expect(result).toMatchObject({ subtype: 'success' })
    const call = fake.messageCalls().at(-1)!
    // The definition's own prompt (plugins/scadbuddy/agents/model-author.md) is the system prompt.
    expect(JSON.stringify(call.body?.system)).toContain('You author ScadBuddy templates')
    // Claude Code sends effort only for a model that takes it (claude-sonnet-4-5 does not, measured).
    expect(JSON.stringify(call.body)).toContain('"output_config":{"effort":"low"}')
  })

  it('recalls into the model’s context at the prompt and retains the transcript at the end (memory hooks)', async () => {
    const hindsight = await startFakeHindsight()
    try {
      hindsight.memories = ['The user prints boxes in PETG.']
      script = () => ({ text: 'Hello from the fake.' })
      const stderrLines: string[] = []
      const memory = createMemoryHooks({
        client: new HindsightClient({ apiBase: `http://hindsight.invalid:${hindsight.port}`, bankId: 'b', address: '127.0.0.1' }),
        secrets: [GATEWAY_TOKEN],
        log: (line) => stderrLines.push(line),
      })
      const sessionId = randomUUID()
      const { result } = await collect({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'Say hello',
        model: 'claude-sonnet-4-5',
        maxTurns: 1,
        sessionId,
        memoryHooks: memory.hooks,
      })
      expect(result).toMatchObject({ subtype: 'success' })
      await memory.settled()
      expect(stderrLines).toEqual([])
      // Recall ran against the prompt, and the model saw the memory, inside the envelope.
      expect(hindsight.recalls().map((r) => (r.body as { query: string }).query)).toEqual(['Say hello'])
      const sent = JSON.stringify(fake.messageCalls().at(-1)?.body?.messages)
      expect(sent).toContain('The user prints boxes in PETG.')
      expect(sent).toContain(UNTRUSTED_KEY)
      // Retain upserted the conversation, without the injected memories. The reply's line
      // has a timestamp when the transcript already held it at Stop, none when it came from
      // `last_assistant_message`; Claude Code 2.1.309 does either.
      const [retain] = hindsight.retains()
      const item = (retain!.body as { items: { document_id: string; content: string }[] }).items[0]!
      expect(item.document_id).toBe(`conversation:${sessionId}`)
      expect(item.content).toContain('{"role":"user","content":"Say hello"')
      expect(item.content).toContain('{"role":"assistant","content":"Hello from the fake."')
      expect(item.content.split('"content":"Hello from the fake."')).toHaveLength(2)
      expect(item.content).not.toContain('PETG')
      expect(item.content).not.toContain(GATEWAY_TOKEN)
    } finally {
      await hindsight.close()
    }
  })

  it('routes an in-process MCP tool call through the permission seam and runs it', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'The tool said echo:hi.' }
        : { toolUse: { name: 'mcp__stub__echo', input: { text: 'hi' } } }
    const decisions: [string, ToolDecision][] = []
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Echo hi',
      model: 'claude-sonnet-4-5',
      mcpServers: { stub: stubServer() },
      tierOf: (name) => tiers[name],
      onDecision: (name, d) => decisions.push([name, d]),
    })
    expect(result).toMatchObject({ subtype: 'success', result: 'The tool said echo:hi.' })
    expect(decisions).toEqual([['mcp__stub__echo', { decision: 'allow', tier: 'read' }]])
    expect(handled).toEqual(['echo:hi'])
    // Only the stub's tools were offered.
    const offered = new Set(fake.messageCalls().flatMap((c) => (c.body?.tools ?? []).map((t) => t.name)))
    expect([...offered].sort()).toEqual(['mcp__stub__echo', 'mcp__stub__print'])
  })

  it('does not run an outward tool: it is denied as needing approval', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'I could not print.' }
        : { toolUse: { name: 'mcp__stub__print', input: { job: 'box.3mf' } } }
    const decisions: [string, ToolDecision][] = []
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Print it',
      model: 'claude-sonnet-4-5',
      mcpServers: { stub: stubServer() },
      tierOf: (name) => tiers[name],
      onDecision: (name, d) => decisions.push([name, d]),
    })
    expect(handled).toEqual([])
    expect(decisions.map(([name, d]) => [name, d.decision])).toEqual([['mcp__stub__print', 'needs_approval']])
    // The model was told why, in the tool result.
    const followUp = fake.messageCalls().at(-1)
    expect(followUp && lastContent(followUp)).toMatch(/needs a human approval in the ScadBuddy UI/)
    expect(result.subtype).toBe('success')
    expect(result.permission_denials.map((d) => d.tool_name)).toEqual(['mcp__stub__print'])
  })

  // #5018: a decision that throws must refuse the call. On #5017 a raising hook callback
  // made Claude Code run the call (the Python SDK); here the tier resolver throws for the
  // one tool the turn calls.
  it('refuses a call whose tier resolver throws, in the hook and in canUseTool (#5018)', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'Refused.' }
        : { toolUse: { name: 'mcp__stub__echo', input: { text: 'hi' } } }
    const decisions: [string, ToolDecision][] = []
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Echo hi',
      model: 'claude-sonnet-4-5',
      mcpServers: { stub: stubServer() },
      tierOf: (name) => {
        if (name === 'mcp__stub__echo') throw new Error('tier table unavailable')
        return tiers[name]
      },
      onDecision: (name, d) => decisions.push([name, d]),
    })
    expect(handled).toEqual([])
    expect(decisions.map(([name, d]) => [name, d.decision])).toEqual([['mcp__stub__echo', 'deny']])
    const followUp = fake.messageCalls().at(-1)
    expect(followUp && lastContent(followUp)).toMatch(/mcp__stub__echo is refused: its access could not be checked/)
    expect(result.permission_denials.map((d) => d.tool_name)).toEqual(['mcp__stub__echo'])
  })

  // Measured before the fix (#5018): Claude Code passed over the throwing hook, and
  // canUseTool, whose resolver answered, ran the call.
  it('refuses a call whose resolver throws only in the hook, though canUseTool would allow it (#5018)', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'Refused.' }
        : { toolUse: { name: 'mcp__stub__echo', input: { text: 'hi' } } }
    let calls = 0
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Echo hi',
      model: 'claude-sonnet-4-5',
      mcpServers: { stub: stubServer() },
      tierOf: (name) => {
        calls += 1
        if (calls === 1) throw new Error('tier table unavailable')
        return tiers[name]
      },
    })
    expect(handled).toEqual([])
    expect(result.permission_denials.map((d) => d.tool_name)).toEqual(['mcp__stub__echo'])
  })

  it('makes a built-in tool impossible: a scripted Bash call never runs', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'No shell here.' }
        : { toolUse: { name: 'Bash', input: { command: 'touch pwned' } } }
    const decisions: string[] = []
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Run a command',
      model: 'claude-sonnet-4-5',
      tierOf: () => 'read', // even a resolver that would allow it cannot
      onDecision: (name) => decisions.push(name),
    })
    expect(result.subtype).toBe('success')
    const followUp = fake.messageCalls().at(-1)
    expect(followUp && lastContent(followUp)).toMatch(/"is_error":true/)
    expect(decisions).toEqual([])
  })

  // Claude Code's own WebFetch connects wherever it is told (measured on 2.1.296: a
  // call to 127.0.0.1:9 tried to connect). An approval does not let it reach a cloud
  // metadata address: the egress hook denies the call before the gate is asked.
  it("refuses an approved WebFetch to a metadata address before Claude Code fetches it", async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'Refused.' }
        : { toolUse: { name: 'WebFetch', input: { url: 'http://169.254.169.254/latest/meta-data/', prompt: 'keys?' } } }
    const asked: string[] = []
    // Built-ins are offered only beside a package an admin allowed as it is.
    const allowed = path.join(stateDir, 'allowed')
    await mkdir(path.join(allowed, '.claude-plugin'), { recursive: true })
    await writeFile(path.join(allowed, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'fetcher' }))
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Fetch it',
      model: 'claude-sonnet-4-5',
      allowedPluginPaths: [allowed],
      builtinTools: ['WebFetch'],
      approvalGate: (request) => {
        asked.push(request.toolName)
        return Promise.resolve({ approved: true, input: request.input })
      },
    })
    expect(result.subtype).toBe('success')
    const followUp = fake.messageCalls().at(-1)
    expect(followUp && lastContent(followUp)).toMatch(/WebFetch refused: url host 169\.254\.169\.254 resolves to 169\.254\.169\.254, a link-local/)
    expect(asked).toEqual([])
  })

  it('stops at maxTurns', async () => {
    script = () => ({ toolUse: { name: 'mcp__stub__echo', input: { text: 'again' } } })
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Loop',
      model: 'claude-sonnet-4-5',
      maxTurns: 2,
      mcpServers: { stub: stubServer() },
      tierOf: (name) => tiers[name],
    })
    expect(result.subtype).toBe('error_max_turns')
  })

  it('stops at the budget', async () => {
    script = () => ({ toolUse: { name: 'mcp__stub__echo', input: { text: 'again' } } })
    const { result } = await collect({
      paths: { stateDir },
      credential: gateway(),
      prompt: 'Loop',
      model: 'claude-sonnet-4-5',
      maxTurns: 50,
      maxBudgetUsd: 0.000001,
      mcpServers: { stub: stubServer() },
      tierOf: (name) => tiers[name],
    })
    expect(result.subtype).toBe('error_max_budget_usd')
  })

  it('stops when aborted mid-request', async () => {
    const stop = new AbortController()
    script = () => {
      setTimeout(() => stop.abort(), 50)
      return { hang: true }
    }
    const messages: SDKMessage[] = []
    const started = performance.now()
    const run = (async () => {
      for await (const m of runHarness({
        paths: { stateDir },
        credential: gateway(),
        prompt: 'hi',
        model: 'claude-sonnet-4-5',
        signal: stop.signal,
      })) {
        messages.push(m)
      }
    })()
    // Interrupted first (run.ts stopFirst, #1168): Claude Code ends the turn
    // with an error result, and the SDK throws on it.
    await expect(run).rejects.toThrow(/error result/i)
    expect(messages.find((m) => m.type === 'result')).toMatchObject({ subtype: 'error_during_execution' })
    expect(fake.messageCalls()).toHaveLength(1)
    expect(performance.now() - started).toBeLessThan(10_000)
  })

  describe('testConnection', () => {
    it('passes with a working credential in one turn', async () => {
      script = () => ({ text: 'ok' })
      const outcome = await testConnection(gateway(), { paths: { stateDir }, model: 'claude-sonnet-4-5' })
      expect(outcome).toMatchObject({ ok: true, detail: 'connected', model: 'claude-sonnet-4-5' })
      expect(fake.messageCalls()).toHaveLength(1)
    })

    it('fails with a rejected credential, and never echoes it', async () => {
      script = () => ({
        error: { status: 401, type: 'authentication_error', message: `invalid token ${GATEWAY_TOKEN}` },
      })
      const outcome = await testConnection(gateway(), { paths: { stateDir }, model: 'claude-sonnet-4-5', timeoutMs: 60_000 })
      expect(outcome).toMatchObject({ ok: false, detail: expect.stringMatching(/refused the request \(HTTP 401/) })
      expect(JSON.stringify(outcome)).not.toContain(GATEWAY_TOKEN)
      // Answered at the first retry, not after Claude Code's whole backoff.
      expect(outcome.duration_ms).toBeLessThan(20_000)
    }, 30_000)
  })
})
