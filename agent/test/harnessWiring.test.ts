import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage, SDKResultMessage, SDKSystemMessage } from '@anthropic-ai/claude-agent-sdk'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { harnessPrincipal } from '../src/auth/principal.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { ApprovalGate, ToolDecision } from '../src/harness/permissions.js'
import { BUNDLED_PLUGIN_DIR, bundledPluginPaths } from '../src/harness/plugins.js'
import { type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import type { ToolServices } from '../src/tools/registry.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { browser } from './support/sessions.js'

// The harness as main.ts wires it (#255, #299): the registry's in-process
// server and tiers (tools/harness.ts) and the bundled ScadBuddy plugin
// (harness/plugins.ts bundledPluginPaths), run through the real SDK and its
// bundled Claude Code against the fake Anthropic endpoint. The backend is msw.

const BACKEND = 'http://backend.test'
const GATEWAY_TOKEN = 'gw-wiring-test-token-777788889999'
const MODELS = [{ slug: 'keychain', name: 'Keychain', description: '', tags: [], origin: 'mine' }]

let deletes = 0
const server = setupServer(
  http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json(MODELS)),
  http.delete(`${BACKEND}/api/v1/models/:slug`, () => {
    deletes += 1
    return new HttpResponse(null, { status: 204 })
  }),
)
// Claude Code is another process; only the in-process tools' backend calls pass through here.
beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function services(): ToolServices {
  return {
    backend: createBackendClient(BACKEND, (request) => fetch(request)),
    pending: new PendingActionStore(),
    pollIntervalMs: 5,
    renderWaitMs: 5000,
  }
}

describe('bundledPluginPaths', () => {
  it("is the repository's plugins/scadbuddy in a checkout, and it passes vetting", () => {
    expect(BUNDLED_PLUGIN_DIR).toBe(path.resolve('../plugins/scadbuddy'))
    const log: string[] = []
    expect(bundledPluginPaths((m) => log.push(m))).toEqual([BUNDLED_PLUGIN_DIR])
    expect(log).toEqual([])
  })

  it('leaves out a missing or refused plugin and says why', () => {
    const log: string[] = []
    expect(bundledPluginPaths((m) => log.push(m), '/nonexistent/scadbuddy')).toEqual([])
    expect(bundledPluginPaths((m) => log.push(m), 'test/fixtures/plugins/command-hook')).toEqual([])
    expect(log).toEqual([
      expect.stringContaining('/nonexistent/scadbuddy is not loaded: not a directory'),
      expect.stringMatching(/command-hook is not loaded: .*hook/),
    ])
  })
})

describe('harnessTools', () => {
  it('maps every registry tool to its risk under its harness name, and nothing else', () => {
    const { tierOf } = harnessTools(services())
    for (const tool of ALL_TOOLS) expect(tierOf(`mcp__scadbuddy__${tool.name}`), tool.name).toBe(tool.risk)
    expect(tierOf('list_models')).toBeUndefined()
    expect(tierOf('mcp__other__list_models')).toBeUndefined()
  })

  it("runs the browser user's session with every tier, and anyone else's with read only", () => {
    expect(harnessPrincipal(browser).tiers).toEqual(['read', 'write', 'outward'])
    expect(harnessPrincipal({ kind: 'bearer', id: 'token:a', label: 'A' }).tiers).toEqual(['read'])
  })
})

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

describe.skipIf(cliMissing !== undefined)(`the wired harness against a fake Anthropic endpoint${cliMissing ? ` (skipped: ${cliMissing})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let stateDir: string

  beforeEach(async () => {
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'wiring-'))
    await ensureStateDirs({ stateDir })
    fake = await startFakeAnthropic((r) => script(r))
    deletes = 0
  })
  afterEach(async () => {
    await fake.close()
  })

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  /** One run as a browser-owned session's turn gets it (sessions/manager.ts runTurn). */
  async function collect(extra: Partial<HarnessRun> = {}) {
    const wired = harnessTools(services())
    const decisions: [string, ToolDecision['decision']][] = []
    const messages: SDKMessage[] = []
    const stderr: string[] = []
    try {
      for await (const m of runHarness({
        paths: { stateDir },
        credential: { kind: 'gateway', baseUrl: fake.url, secret: GATEWAY_TOKEN },
        prompt: 'Go',
        model: 'claude-sonnet-4-5',
        tierOf: wired.tierOf,
        mcpServers: wired.mcpServers({ owner: browser }),
        pluginPaths: bundledPluginPaths((m) => stderr.push(m)),
        onDecision: (name, d) => decisions.push([name, d.decision]),
        stderr: (l) => stderr.push(l),
        ...extra,
      })) {
        messages.push(m)
      }
    } catch (err) {
      if (!messages.some((m) => m.type === 'result')) throw err
    }
    const result = messages.find((m): m is SDKResultMessage => m.type === 'result')
    if (!result) throw new Error(`no result message; stderr: ${stderr.join('')}`)
    const init = messages.find((m): m is SDKSystemMessage => m.type === 'system' && m.subtype === 'init')
    if (!init) throw new Error('no init message')
    return { result, init, decisions, stderr }
  }

  // One query for both, to spawn Claude Code as few times as the suite can.
  it('reports the ScadBuddy plugin loaded with no plugin errors, and runs a read tool within its tier', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'done' }
        : { toolUse: { name: 'mcp__scadbuddy__list_models', input: {} } }
    const { result, init, decisions } = await collect()
    expect(result.subtype).toBe('success')
    expect(decisions).toEqual([['mcp__scadbuddy__list_models', 'allow']])
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('keychain')
    // Beside Claude Code's own built-in ones (`agents-md@builtin` on 2.1.283).
    expect(init.plugins.filter((p) => p.path !== 'builtin')).toEqual([
      expect.objectContaining({ name: 'scadbuddy', path: BUNDLED_PLUGIN_DIR, version: '0.1.0' }),
    ])
    expect(init.plugin_errors).toBeUndefined()
    // Its skills are there; its .mcp.json (the remote server for Claude Code
    // installs) is not started: strictMcpConfig (harness/plugins.ts).
    expect(init.skills).toEqual(expect.arrayContaining(['scadbuddy:authoring', 'scadbuddy:customize', 'scadbuddy:print']))
    expect(init.mcp_servers.map((s) => [s.name, s.status])).toEqual([['scadbuddy', 'connected']])
    // Every registry tool is offered, by its harness name, and no built-in.
    expect([...init.tools].sort()).toEqual(ALL_TOOLS.map((t) => `mcp__scadbuddy__${t.name}`).sort())
  }, 60_000)

  it('parks an outward tool at the gate, and once approved runs it (not a second prepare)', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'deleted' }
        : { toolUse: { name: 'mcp__scadbuddy__delete_model', input: { slug: 'keychain' } } }
    const asked: string[] = []
    const approve: ApprovalGate = async (request) => {
      asked.push(request.toolName)
      return { approved: true, input: request.input }
    }
    const { result } = await collect({ approvalGate: approve })
    expect(result.subtype).toBe('success')
    expect(asked).toEqual(['mcp__scadbuddy__delete_model'])
    const followUp = lastContent(fake.messageCalls().at(-1)!)
    expect(deletes).toBe(1)
    expect(followUp).toContain('deleted')
    expect(followUp).not.toContain('pending_approval')
  }, 60_000)

  it('never runs an outward tool without a gate', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'could not' }
        : { toolUse: { name: 'mcp__scadbuddy__delete_model', input: { slug: 'keychain' } } }
    const { result, decisions } = await collect()
    expect(result.subtype).toBe('success')
    expect(decisions).toEqual([['mcp__scadbuddy__delete_model', 'needs_approval']])
    expect(deletes).toBe(0)
    expect(lastContent(fake.messageCalls().at(-1)!)).toMatch(/needs a human approval in the ScadBuddy UI/)
  }, 60_000)
})
