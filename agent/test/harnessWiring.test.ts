import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
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
import { ASK_USER_QUESTION, ASK_USER_TOOL, type QuestionGate, type QuestionRequest } from '../src/harness/questions.js'
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

    // #946: a subagent asked to run in the background runs inside the turn.
    // Backgrounded, it outlived its parent's turn: the SDK closes Claude Code's
    // input at a string prompt's first result, and from then on Claude Code
    // refused every permission request itself ("The user doesn't want to take
    // this action right now"), asking neither canUseTool nor the user. That hit
    // the subagent's own calls and those of the turn Claude Code starts when it
    // reports back, read tools included (measured on Claude Code 2.1.283).
    describe('a subagent asked to run in the background (#946)', () => {
      /** The subagent calls `tool` once; returns what the parent and the subagent got back. */
      function backgroundScript(tool: string, input: Record<string, unknown>) {
        const parent: string[] = []
        const subagent: string[] = []
        script = (r) => {
          const last = lastContent(r)
          if (JSON.stringify(r.body?.messages?.[0] ?? '').includes('BG-TASK')) {
            if (!last.includes('tool_result')) return { toolUse: { name: tool, input } }
            subagent.push(last)
            return { text: 'BG-DONE' }
          }
          if (last.includes('tool_result')) {
            parent.push(last)
            return { text: 'done' }
          }
          return {
            toolUse: {
              name: 'Agent',
              input: { subagent_type: 'scadbuddy:model-author', description: 'Background', prompt: 'BG-TASK', run_in_background: true },
            },
          }
        }
        return { parent, subagent }
      }

      it('runs a read call through canUseTool, and the parent gets its result', async () => {
        const { parent, subagent } = backgroundScript('mcp__scadbuddy__list_models', {})
        const { result, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR })
        expect(result.subtype).toBe('success')
        expect(decisions).toEqual([['mcp__scadbuddy__list_models', 'allow']])
        expect(subagent.join('')).toContain('keychain')
        // The parent waited for the subagent instead of being told it was launched.
        expect(parent.join('')).toContain('BG-DONE')
        expect(parent.join('')).not.toContain('Async agent launched')
      }, 60_000)

      it('parks an outward call at the gate', async () => {
        const { parent } = backgroundScript('mcp__scadbuddy__delete_model', { slug: 'keychain' })
        const asked: string[] = []
        const gate: ApprovalGate = (request) => {
          asked.push(request.toolName)
          return Promise.resolve({ approved: true, input: request.input })
        }
        const { result, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR, approvalGate: gate })
        expect(result.subtype).toBe('success')
        expect(decisions).toEqual([['mcp__scadbuddy__delete_model', 'needs_approval']])
        expect(asked).toEqual(['mcp__scadbuddy__delete_model'])
        expect(deletes).toBe(1)
        expect(parent.join('')).toContain('BG-DONE')
      }, 60_000)
    })

    // #940: a subagent asks the user too. Claude Code refuses AskUserQuestion
    // inside a subagent ("AskUserQuestion is not available inside subagents",
    // measured on Claude Code 2.1.283) and never hands the call to canUseTool,
    // so a session with a question gate also gets ASK_USER_TOOL, an in-process
    // MCP tool that parks on the same gate; a subagent's MCP calls reach the
    // host like any other.
    describe("a subagent's question (#940)", () => {
      const QUESTIONS = [
        {
          question: 'Which colour should the base be?',
          header: 'Colour',
          multiSelect: false,
          options: [
            { label: 'Red', description: 'PLA Basic red' },
            { label: 'Blue', description: 'PLA Basic blue' },
          ],
        },
      ]

      /** The subagent calls `tool` once; returns what the parent and the subagent got back. */
      function subagentAsks(tool: string) {
        const parent: string[] = []
        const subagent: string[] = []
        script = (r) => {
          const last = lastContent(r)
          if (JSON.stringify(r.body?.messages?.[0] ?? '').includes('ASK-TASK')) {
            if (!last.includes('tool_result')) return { toolUse: { name: tool, input: { questions: QUESTIONS } } }
            subagent.push(last)
            return { text: 'SUB-DONE' }
          }
          if (last.includes('tool_result')) {
            parent.push(last)
            return { text: 'done' }
          }
          return {
            toolUse: { name: 'Agent', input: { subagent_type: 'scadbuddy:model-author', description: 'Ask', prompt: 'ASK-TASK' } },
          }
        }
        return { parent, subagent }
      }

      function recordingGate(verdict: Awaited<ReturnType<QuestionGate>>) {
        const asked: QuestionRequest[] = []
        const gate: QuestionGate = (request) => {
          asked.push(request)
          return Promise.resolve(verdict)
        }
        return { asked, gate }
      }

      it('Claude Code refuses AskUserQuestion in a subagent before the gate sees it', async () => {
        const { subagent, parent } = subagentAsks(ASK_USER_QUESTION)
        const { asked, gate } = recordingGate({ answered: true, answers: { 'Which colour should the base be?': 'Blue' } })
        const { result, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR, questionGate: gate })
        expect(result.subtype).toBe('success')
        expect(asked).toEqual([])
        expect(decisions).toEqual([])
        expect(subagent.join('')).toContain('AskUserQuestion is not available inside subagents')
        expect(parent.join('')).toContain('SUB-DONE')
      }, 60_000)

      it("parks the subagent's ask_user call on the gate and hands it the answer", async () => {
        const { subagent, parent } = subagentAsks(ASK_USER_TOOL)
        const { asked, gate } = recordingGate({ answered: true, answers: { 'Which colour should the base be?': 'Blue' } })
        const { result, init, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR, questionGate: gate })
        expect(result.subtype).toBe('success')
        expect(init.tools).toContain(ASK_USER_TOOL)
        expect(decisions).toEqual([[ASK_USER_TOOL, 'allow']])
        expect(asked).toHaveLength(1)
        expect(asked[0]?.questions).toEqual(QUESTIONS)
        // The tool_use block's id, as for AskUserQuestion: the panel's tool.call id.
        expect(asked[0]?.toolUseId).toMatch(/^toolu_/)
        const got = subagent.join('')
        expect(got).toContain('Which colour should the base be?')
        expect(got).toContain('Blue')
        expect(got).not.toMatch(/"is_error":true/)
        expect(parent.join('')).toContain('SUB-DONE')
      }, 60_000)

      it('an unanswered question reaches the subagent as the tool error, never as an answer', async () => {
        const { subagent } = subagentAsks(ASK_USER_TOOL)
        const { gate } = recordingGate({ answered: false, message: 'The user did not answer: the turn stopped first.' })
        const { result } = await collect({ ownPlugin: OWN_PLUGIN_DIR, questionGate: gate })
        expect(result.subtype).toBe('success')
        expect(subagent.join('')).toContain('The user did not answer')
        expect(subagent.join('')).toMatch(/"is_error":true/)
      }, 60_000)

      it('is not offered without a gate', async () => {
        script = () => ({ text: 'Nothing to ask.' })
        const { init } = await collect({ ownPlugin: OWN_PLUGIN_DIR })
        expect(init.tools).not.toContain(ASK_USER_TOOL)
      }, 60_000)
    })

    // With Skill and Agent offered, another loaded plugin's subagents run too
    // (an approved package's, #297). One that asks for a built-in gets none:
    // the session offers only Skill and Agent (docs/ai/security.md, "Plugin packages").
    it("gives another plugin's subagent no built-in beyond the session's", async () => {
      const other = path.join(stateDir, 'other')
      await mkdir(path.join(other, '.claude-plugin'), { recursive: true })
      await mkdir(path.join(other, 'agents'), { recursive: true })
      await writeFile(path.join(other, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'other' }))
      await writeFile(
        path.join(other, 'agents', 'shell.md'),
        '---\nname: shell\ndescription: Runs commands.\ntools: Bash, mcp__scadbuddy\n---\n\nRun what you are asked.\n',
      )
      const asked: RecordedRequest[] = []
      script = (r) => {
        const last = lastContent(r)
        if (last.includes('SHELL-TASK') && !last.includes('tool_result')) {
          return { toolUse: { name: 'Bash', input: { command: 'echo pwned' } } }
        }
        if (last.includes('tool_result')) {
          asked.push(r)
          return { text: 'done' }
        }
        return { toolUse: { name: 'Agent', input: { subagent_type: 'other:shell', description: 'Run', prompt: 'SHELL-TASK' } } }
      }
      const { result, init, decisions } = await collect({ ownPlugin: OWN_PLUGIN_DIR, pluginPaths: [other] })
      expect(result.subtype).toBe('success')
      expect(init.agents).toContain('other:shell')
      expect(init.tools).not.toContain('Bash')
      expect(decisions.filter(([name]) => name === 'Bash')).toEqual([])
      // The subagent's Bash call came back as an error, not a command's output.
      const results = asked.map(lastContent).join('')
      expect(results).toContain('No such tool available: Bash')
    }, 60_000)
  })
})
