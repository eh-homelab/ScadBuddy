import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage, SDKResultMessage, SDKSystemMessage } from '@anthropic-ai/claude-agent-sdk'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { harnessPrincipal } from '../src/auth/principal.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { OWN_PLUGIN_DIR } from '../src/harness/ownPlugin.js'
import type { ApprovalGate, ToolDecision } from '../src/harness/permissions.js'
import { type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import type { ToolServices } from '../src/tools/registry.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import type { Owner } from '../src/sessions/protocol.js'
import { browser } from './support/sessions.js'

// The harness as main.ts wires it (#255): the registry's in-process server and
// tiers (tools/harness.ts), run through the real SDK and its bundled Claude
// Code against the fake Anthropic endpoint. The backend is msw. A run without
// ScadBuddy's own plugin has no built-in tool; main.ts passes the plugin
// (#896), which brings the Skill and Agent tools and nothing else.

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

  async function offered(owner: Owner): Promise<string[]> {
    const server = harnessTools(services()).mcpServers({ owner }).scadbuddy!
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await server.instance.connect(serverSide)
    const client = new Client({ name: 'wiring-test', version: '0' })
    await client.connect(clientSide)
    const { tools } = await client.listTools()
    await client.close()
    return tools.map((t) => t.name).sort()
  }

  // Local review of #526, finding 3: a tool the principal cannot run is not
  // offered, so its call is never parked for an approval that cannot help.
  it("offers the browser user every tool, and anyone else's session only the read ones", async () => {
    expect(await offered(browser)).toEqual(ALL_TOOLS.map((t) => t.name).sort())
    const reads = ALL_TOOLS.filter((t) => t.risk === 'read').map((t) => t.name).sort()
    expect(reads.length).toBeGreaterThan(0)
    expect(reads.length).toBeLessThan(ALL_TOOLS.length)
    expect(await offered({ kind: 'bearer', id: 'token:a', label: 'A' })).toEqual(reads)
    expect(await offered({ kind: 'flow', id: 'flow:x', label: 'X' })).toEqual(reads)
    expect(await offered({ kind: 'bearer', id: 'token:a', label: 'A' })).not.toContain('delete_model')
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
  it('offers only the registry tools (no Skill, no Agent, no plugin) and runs a read tool within its tier', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result')
        ? { text: 'done' }
        : { toolUse: { name: 'mcp__scadbuddy__list_models', input: {} } }
    const { result, init, decisions } = await collect()
    expect(result.subtype).toBe('success')
    expect(decisions).toEqual([['mcp__scadbuddy__list_models', 'allow']])
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('keychain')
    // Claude Code's own built-in ones only (`agents-md@builtin` on 2.1.283).
    expect(init.plugins.filter((p) => p.path !== 'builtin')).toEqual([])
    expect(init.skills.filter((s) => s.startsWith('scadbuddy:'))).toEqual([])
    expect(init.mcp_servers.map((s) => [s.name, s.status])).toEqual([['scadbuddy', 'connected']])
    // Every registry tool is offered, by its harness name, and no built-in:
    // nothing that could invoke a skill or a subagent.
    expect([...init.tools].sort()).toEqual(ALL_TOOLS.map((t) => `mcp__scadbuddy__${t.name}`).sort())
    expect(init.tools).not.toContain('Skill')
    expect(init.tools).not.toContain('Agent')
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

  describe("ScadBuddy's own plugin (#896)", () => {
    const registry = ALL_TOOLS.map((t) => `mcp__scadbuddy__${t.name}`)

    it('loads its skills and subagents, and offers Skill and Agent beside the registry tools', async () => {
      script = (r) =>
        lastContent(r).includes('tool_result')
          ? { text: 'done' }
          : { toolUse: { name: 'Skill', input: { skill: 'scadbuddy:customize' } } }
      const { result, init, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR })
      expect(result.subtype).toBe('success')
      expect(init.plugins).toContainEqual(expect.objectContaining({ name: 'scadbuddy', path: OWN_PLUGIN_DIR }))
      expect(init.plugin_errors ?? []).toEqual([])
      expect(init.skills.filter((s) => s.startsWith('scadbuddy:')).sort()).toEqual(
        ['scadbuddy:authoring', 'scadbuddy:customize', 'scadbuddy:print'],
      )
      expect(init.agents).toEqual(expect.arrayContaining(['scadbuddy:model-author', 'scadbuddy:print-analyst']))
      // `Agent` is listed by its older name (measured on Claude Code 2.1.283).
      expect([...init.tools].sort()).toEqual(['Skill', 'Task', ...registry].sort())
      // No server of its own: its tools are the in-process `scadbuddy` server.
      expect(init.mcp_servers.map((s) => s.name)).toEqual(['scadbuddy'])
      // Claude Code asks no permission for Skill (the PreToolUse hook passes it
      // at `read`), so canUseTool records nothing.
      expect(decisions).toEqual([])
      // The skill's body reached the model.
      expect(lastContent(fake.messageCalls().at(-1)!)).toContain('get_schema')
    }, 60_000)

    it("runs a subagent whose calls go through the session's own permission seam", async () => {
      script = (r) => {
        const last = lastContent(r)
        if (last.includes('tool_result')) return { text: 'done' }
        if (last.includes('SUBAGENT-TASK')) return { toolUse: { name: 'mcp__scadbuddy__delete_model', input: { slug: 'keychain' } } }
        return {
          toolUse: {
            name: 'Agent',
            input: { subagent_type: 'scadbuddy:model-author', description: 'Delete it', prompt: 'SUBAGENT-TASK delete keychain' },
          },
        }
      }
      const { result, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR })
      expect(result.subtype).toBe('success')
      // The subagent ran (only it calls delete_model), and its outward call is
      // no more allowed than the session's: no gate, so denied.
      expect(decisions).toEqual([['mcp__scadbuddy__delete_model', 'needs_approval']])
      expect(deletes).toBe(0)
    }, 60_000)
  })
})
