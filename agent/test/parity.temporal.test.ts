import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { TabHub } from '../src/bridge/hub.js'
import { PostgresPairingStore } from '../src/bridge/pairings.js'
import type { Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { HTTP_TOOL_NAME } from '../src/harness/httpRequest.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { forwardForRun, PluginForwarder, type PluginsForRun } from '../src/plugins/forwarder.js'
import type { PackagesForRun } from '../src/plugins/packages/install.js'
import type { CheckedPlugin } from '../src/plugins/registry.js'
import { respond } from '../src/routes/pendingInput.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { services } from './helpers/mcp.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type FakeHindsight, startFakeHindsight } from './support/fakeHindsight.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// Plan Step 6.3's parity pins (#2320, #2660): an AgentSession turn, on the real SDK and its
// bundled Claude Code (pointed at the local fake Anthropic endpoint), gets every tool a classic
// turn gets, and so does a subagent it starts. The turn runs as a worker replica runs it
// (support/agentSessions.ts): the agent-sessions workflow, the session host queue, TurnHost.
//
//   - a remote MCP plugin, through the loopback forwarder (#297);
//   - an approved plugin package's skill and subagent, and an `allow_refused` package's named
//     built-ins, every one of them (not only Bash);
//   - http_request (#827);
//   - the browser_* tools (#254; bridge.e2e's calls reach a paired tab on the same stack);
//   - Hindsight's automatic memory: the recall before the prompt and the retain after it.
//
// A subagent gets the turn's plugin, http_request and browser tools, and its outward call parks
// for the person, as the main thread's does (#2660).

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const TOKEN = 'gw-parity-e2e-token-1212343456567878'
const SUBAGENT_MARK = 'PARITY-SUBAGENT-5d1e'
/** The built-ins the allowed package names (vet.ts `builtin_tools`). */
const BUILTINS = ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebFetch']
const BROWSER_TOOLS = ALL_TOOLS.filter((t) => t.name.startsWith('browser_'))
const PLUGIN_TOOLS = ['mcp__my-memory__recall', 'mcp__my-memory__retain']

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(`an AgentSession turn's tools${skip ? ` (skipped: ${skip})` : ''}`, () => {
  agentSessions()
  let forwarder: PluginForwarder
  let fake: FakeAnthropic
  let script: (r: RecordedRequest) => Reply
  let mcp: FakeMcp
  let hindsight: FakeHindsight
  let db: Database
  let drop: () => Promise<void>
  let dir: string

  beforeAll(async () => {
    forwarder = await PluginForwarder.start()
  })
  afterAll(async () => {
    await forwarder.close()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    fake = await startFakeAnthropic((r) => script(r))
    mcp = await startFakeMcp()
    hindsight = await startFakeHindsight()
    dir = await mkdtemp(path.join(os.tmpdir(), 'parity-'))
  })
  afterEach(async () => {
    await stopManagers()
    await fake.close()
    await mcp.close()
    await hindsight.close()
    await drop()
    await rm(dir, { recursive: true, force: true })
  })

  /** A plugin package directory: `files` by path under it. */
  async function pluginDir(name: string, files: Record<string, string>): Promise<string> {
    const root = path.join(dir, name)
    for (const [rel, content] of Object.entries({ '.claude-plugin/plugin.json': JSON.stringify({ name, version: '1.0.0' }), ...files })) {
      await mkdir(path.dirname(path.join(root, rel)), { recursive: true })
      await writeFile(path.join(root, rel), content)
    }
    return root
  }

  async function agent(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    // An approved package with a skill and a subagent, and one approved with allow_refused
    // whose skill names the built-ins.
    const greeter = await pluginDir('greeter', {
      'skills/hello/SKILL.md': '---\nname: hello\ndescription: Greets the user.\n---\n\nGreet the user.\n',
      'agents/helper.md': `---\nname: helper\ndescription: Helps with greetings.\n---\n\nHelp with greetings. ${SUBAGENT_MARK}\n`,
    })
    const shell = await pluginDir('shell', {
      'skills/run/SKILL.md': `---\nname: run\ndescription: Runs things.\nallowed-tools: ${BUILTINS.join(', ')}\n---\n\nRun it.\n`,
    })
    const packagePlugins = (): Promise<PackagesForRun> =>
      Promise.resolve({ paths: [greeter], allowedPaths: [shell], builtinTools: BUILTINS, loaded: [], problems: [], release: () => {} })
    // A remote MCP plugin through the forwarder, and Hindsight beside it (never given to Claude Code).
    const memory: CheckedPlugin = {
      plugin: { name: 'my-memory', url: mcp.url, toolTiers: { recall: 'read' }, disabledTools: [] },
      address: '127.0.0.1',
    }
    const bank: CheckedPlugin = {
      plugin: { name: 'hindsight', url: `http://hindsight.invalid:${hindsight.port}/mcp/parity/`, toolTiers: {}, disabledTools: [] },
      address: '127.0.0.1',
    }
    const remotePlugins = (): Promise<PluginsForRun> => Promise.resolve({ ...forwardForRun({ plugins: [memory], problems: [] }, forwarder), hindsight: bank })
    const tabs = new TabHub({ pairings: new PostgresPairingStore(db.sql), callTimeoutMs: 1_500, pollMs: 100 })
    return manager({
      sql: db.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      ...harnessTools(services({ browser: tabs }), BROWSER_TOOLS),
      remotePlugins,
      packagePlugins,
      httpRequest: {},
      approvalPollMs: 50,
      approvalHashKey: Buffer.alloc(32, 5),
    })
  }

  const tools = (r: RecordedRequest | undefined) => (r?.body?.tools ?? []).map((t) => t.name)
  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const isSubagent = (r: RecordedRequest) => JSON.stringify(r.body?.system ?? '').includes(SUBAGENT_MARK)
  /** The model requests that offered tools: the turn's, not Claude Code's own side requests (a title). */
  const turnCalls = () => fake.messageCalls().filter((r) => tools(r).length > 0)

  it('offers the turn its plugin, packages, http_request, browser tools and every named built-in, and runs Hindsight', async () => {
    script = () => ({ text: 'Done.' })
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect((await m.get(session.id, browser)).runtime).toBe('unified')

    const offered = tools(turnCalls()[0])
    expect(offered).toEqual(expect.arrayContaining([...PLUGIN_TOOLS, HTTP_TOOL_NAME, ...BROWSER_TOOLS.map((t) => `mcp__scadbuddy__${t.name}`)]))
    // The package's skill and subagent: the Skill and Agent tools come with them (#2320).
    expect(offered).toEqual(expect.arrayContaining(['Skill', 'Agent']))
    // Every built-in the allowed package names, not only Bash: Claude Code's own, or ScadBuddy's
    // in its place (src/builtins: WebFetch through the egress check).
    expect(BUILTINS.filter((b) => !offered.includes(b) && !offered.includes(`mcp__builtin__${b}`))).toEqual([])
    // Hindsight: recalled against the user's words before the prompt, and the turn retained after it.
    expect(hindsight.recalls().map((r) => (r.body as { query: string }).query)).toEqual(['make a box'])
    await expect.poll(() => hindsight.retains().length).toBeGreaterThan(0)
  }, 120_000)

  it("gives a subagent the turn's plugin, http_request and browser tools, and its outward call parks", async () => {
    script = (r) => {
      const last = lastContent(r)
      if (isSubagent(r)) {
        return last.includes('tool_result') ? { text: 'The helper is done.' } : { toolUse: { name: 'mcp__my-memory__retain', input: { text: 'a greeting' } } }
      }
      if (last.includes('tool_result')) return { text: 'All done.' }
      return { toolUse: { name: 'Agent', input: { description: 'Greet', prompt: 'Greet the user.', subagent_type: 'greeter:helper' } } }
    }
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask the helper' })

    // The subagent's outward call parks for the person (the turn's gate, spec §6.6).
    let entry: { request_id: string; tool: string } | undefined
    await expect
      .poll(
        async () => {
          ;[entry] = await db.sql<{ request_id: string; tool: string }[]>`
            SELECT request_id, tool FROM ai_pending_input WHERE session_id = ${session.id}`
          return entry?.tool
        },
        { timeout: 60_000 },
      )
      .toBe('mcp__my-memory__retain')
    expect(mcp.calls).toEqual([])

    const sub = fake.messageCalls().find(isSubagent)
    expect(tools(sub)).toEqual(expect.arrayContaining([...PLUGIN_TOOLS, HTTP_TOOL_NAME, ...BROWSER_TOOLS.map((t) => `mcp__scadbuddy__${t.name}`)]))

    expect(await respond(m, browser, entry!.request_id, { kind: 'approval', decision: 'deny' })).toMatchObject({ outcome: 'denied' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(mcp.calls).toEqual([])
  }, 120_000)
})
