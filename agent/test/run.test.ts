import { randomUUID } from 'node:crypto'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, type SDKMessage, type SDKResultMessage, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { Credential } from '../src/credentials.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { RiskTier, ToolDecision } from '../src/harness/permissions.js'
import { PluginRefusedError } from '../src/harness/plugins.js'
import { buildHarnessOptions, credentialEnv, DEFAULT_MAX_TURNS, type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { testConnection } from '../src/harness/testConnection.js'
import { createMemoryHooks, HindsightClient } from '../src/memory/hindsight.js'
import { UNTRUSTED_KEY } from '../src/safety/untrusted.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { startFakeHindsight } from './support/fakeHindsight.js'

const API_KEY = 'sk-ant-api03-unit-test-key-000011112222'
const GATEWAY_TOKEN = 'gw-unit-test-token-3333444455556666'

describe('buildHarnessOptions', () => {
  const paths = { stateDir: '/var/lib/scadbuddy-agent' }
  const base: HarnessRun = { paths, credential: { kind: 'anthropic_api_key', secret: API_KEY }, prompt: 'hi' }

  it('passes an API key as ANTHROPIC_API_KEY and nothing else', () => {
    expect(credentialEnv({ kind: 'anthropic_api_key', secret: API_KEY })).toEqual({ ANTHROPIC_API_KEY: API_KEY })
  })

  it('passes a gateway as ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN', () => {
    expect(credentialEnv({ kind: 'gateway', baseUrl: 'https://llm.example', secret: GATEWAY_TOKEN })).toEqual({
      ANTHROPIC_BASE_URL: 'https://llm.example',
      ANTHROPIC_AUTH_TOKEN: GATEWAY_TOKEN,
    })
  })

  it('keeps the least-privilege base and puts the credential in env only', () => {
    const options = buildHarnessOptions(base)
    expect(options.tools).toEqual([])
    expect(options.settingSources).toEqual([])
    expect(options.strictMcpConfig).toBe(true)
    expect(options.permissionMode).toBe('default')
    expect(options.allowedTools).toBeUndefined()
    expect(Object.keys(options.env ?? {}).sort()).toEqual(
      [
        'ANTHROPIC_API_KEY',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
        'CLAUDE_CONFIG_DIR',
        'HOME',
        ...(process.env.PATH === undefined ? [] : ['PATH']),
      ].sort(),
    )
    // Nowhere else in the options.
    const { env: _env, ...rest } = options
    expect(JSON.stringify(rest)).not.toContain(API_KEY)
    expect(process.env.ANTHROPIC_API_KEY).not.toBe(API_KEY)
  })

  it('sets the limits, with defaults', () => {
    expect(buildHarnessOptions(base)).toMatchObject({ maxTurns: DEFAULT_MAX_TURNS, maxBudgetUsd: 1 })
    expect(buildHarnessOptions({ ...base, maxTurns: 3, maxBudgetUsd: 0.2 })).toMatchObject({
      maxTurns: 3,
      maxBudgetUsd: 0.2,
    })
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
    expect(options.hooks?.PreToolUse).toHaveLength(1)
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
    expect(options.resume).toBeUndefined()
    // Without them, the service-wide scratch dir and no mirror, as before.
    const plain = buildHarnessOptions(base)
    expect(plain.cwd).toBe('/var/lib/scadbuddy-agent/work')
    expect(plain.sessionStore).toBeUndefined()
    expect(plain.includePartialMessages).toBeUndefined()
  })

  it('refuses a plugin that would start a process with the credential env (spec §8.6)', () => {
    expect(() =>
      buildHarnessOptions({ ...base, pluginPaths: ['../plugins/scadbuddy', 'test/fixtures/plugins/command-hook'] }),
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
    // The credential appears in no message and no stderr line.
    expect(JSON.stringify(messages)).not.toContain(GATEWAY_TOKEN)
    expect(stderr.join('\n')).not.toContain(GATEWAY_TOKEN)
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
      // Retain upserted the conversation, without the injected memories.
      const [retain] = hindsight.retains()
      const item = (retain!.body as { items: { document_id: string; content: string }[] }).items[0]!
      expect(item.document_id).toBe(`conversation:${sessionId}`)
      expect(item.content).toContain('{"role":"user","content":"Say hello"')
      expect(item.content).toContain('{"role":"assistant","content":"Hello from the fake."}')
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
    const started = Date.now()
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
    await expect(run).rejects.toThrow(/abort/i)
    expect(messages.some((m) => m.type === 'result')).toBe(false)
    expect(Date.now() - started).toBeLessThan(10_000)
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
