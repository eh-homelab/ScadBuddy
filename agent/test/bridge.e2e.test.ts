import { fixedCredentials } from './support/fixedCredentials.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { WebSocket } from 'ws'
import { DEFAULT_MCP_AUTH } from '../src/auth/authenticate.js'
import { PostgresTokenStore } from '../src/auth/tokens.js'
import { TabHub } from '../src/bridge/hub.js'
import { PostgresPairingStore } from '../src/bridge/pairings.js'
import type { Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { firstText, services } from './helpers/mcp.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type FrontendBridge, type FrontendTabLink, frontendClientMessages, frontendTabLink } from './support/frontendProtocol.js'
import { type LiveAgent, openPanelSocket, startLiveAgent } from './support/liveAgent.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { manager, tempPaths } from './support/sessions.js'

// The browser bridge end to end (#254): the tab's own code (frontend
// src/agent/bridge.ts and link.ts, loaded as they are) connects over a real
// WebSocket to the agent's real HTTP server (createApp on @hono/node-server
// with `ws`, as main.ts runs it), and the browser_* tools reach it
//
//   - over /mcp, from an MCP client with a bearer token, once the user has
//     typed the pairing code into the tab (spec §8.5; pairings and tokens in
//     Postgres);
//   - from a chat session's turn: the panel's socket names its tab
//     (`tab.bind`), and the real SDK and bundled Claude Code, pointed at the
//     local fake Anthropic endpoint, call mcp__scadbuddy__browser_set_param,
//     which runs the tab's handler and hands its answer back to the model.
//
// The DOM half (a real browser, the app, the Bambuddy iframe) is
// frontend/e2e/agent-link.spec.ts.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const TAB_ID = 'e2e-tab-0123456789abcdef'
const TOKEN = 'gw-bridge-e2e-token-5555666677778888'

async function until<T>(read: () => T | undefined, what: string, timeoutMs = 10_000): Promise<T> {
  const deadline = performance.now() + timeoutMs
  for (;;) {
    const value = read()
    if (value !== undefined && value !== false) return value
    if (performance.now() > deadline) throw new Error(`timed out waiting for ${what}`)
    await new Promise((r) => setTimeout(r, 20))
  }
}

describe.skipIf(skip !== undefined)(`the browser bridge over its real socket${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let agent: LiveAgent | undefined
  let link: FrontendTabLink | undefined
  let fake: FakeAnthropic | undefined
  let script: (r: RecordedRequest) => Reply = () => ({ text: 'ok' })
  let params: Record<string, unknown>
  let bridge: FrontendBridge
  let tabs: TabHub

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    params = { width: 20 }
  })
  afterEach(async () => {
    link?.close()
    link = undefined
    await agent?.close()
    agent = undefined
    await fake?.close()
    fake = undefined
    await drop()
  })

  /** The agent, with /mcp on Postgres tokens and the bridge on Postgres pairings. */
  async function start(): Promise<{ tokens: PostgresTokenStore }> {
    fake = await startFakeAnthropic((r) => script(r))
    tabs = new TabHub({ pairings: new PostgresPairingStore(db.sql), callTimeoutMs: 1_500, pollMs: 100 })
    const toolServices = services({ browser: tabs })
    const tokens = new PostgresTokenStore(db.sql)
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const url = fake.url
    const m = manager({
      sql: db.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      ...harnessTools(toolServices),
      approvalHashKey: Buffer.alloc(32, 9),
    })
    agent = await startLiveAgent(m, {
      database: { ping: () => Promise.resolve(true), ready: db.ready },
      tabs,
      mcp: { tools: ALL_TOOLS, services: toolServices, tokens, authSettings: () => DEFAULT_MCP_AUTH },
    })
    return { tokens }
  }

  /** The tab: the frontend's own AgentBridge with a customizer's handlers, linked by the frontend's own link. */
  async function openTab(): Promise<FrontendTabLink> {
    const { AgentBridge, createTabLink } = await frontendTabLink()
    bridge = new AgentBridge()
    bridge.setRoute('/m/box')
    bridge.register(
      {
        get_params: () => ({ ...params }),
        set_param: ({ name, value }: { name: string; value: unknown }) => {
          if (!(name in params)) throw Object.assign(new Error(`No parameter "${name}".`), { name: 'Error' })
          params[name] = value
          return { name, value }
        },
        // A page that never settles, for the timeout.
        render: () => new Promise(() => {}),
      } as Record<string, (args: never) => unknown>,
      { label: 'customize' },
    )
    const origin = agent!.origin
    class UiSocket extends WebSocket {
      constructor(url: string) {
        // What the browser sends: the UI's own Origin (routes/bridge.ts checks it).
        super(url, { headers: { origin } })
      }
    }
    const created = createTabLink({
      bridge,
      tabId: TAB_ID,
      url: `${agent!.url.replace(/^http/, 'ws')}/api/v1/ai/bridge`,
      WebSocketImpl: UiSocket,
      baseMs: 20,
      maxMs: 100,
    })
    created.connect()
    await until(() => created.getState().connected && tabs.connected().includes(TAB_ID), 'the tab to connect')
    link = created
    return created
  }

  async function mcpClient(token: string): Promise<Client> {
    const client = new Client({ name: 'bridge-e2e', version: '0' })
    await client.connect(
      new StreamableHTTPClientTransport(new URL(`${agent!.url}/mcp`), {
        requestInit: { headers: { authorization: `Bearer ${token}` } },
      }),
    )
    return client
  }

  it('an MCP client pairs by the code the user types in the tab, then drives it and gets its answers', async () => {
    const { tokens } = await start()
    const tab = await openTab()
    const { token } = await tokens.mint({ name: 'laptop', tier: 'outward' })
    const client = await mcpClient(token)
    const call = async (name: string, args: Record<string, unknown> = {}) => {
      const result = (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: { text: string }[] }
      return { isError: result.isError ?? false, body: firstText(result), text: result.content[0]!.text }
    }

    expect((await call('browser_set_param', { name: 'width', value: 30 })).text).toMatch(/^no browser attached/)
    const pair = (await call('browser_pair')).body as { code: string; shown_to_user_as: string }
    expect(pair.shown_to_user_as).toBe('MCP token “laptop”')

    // The tab shows the request (never the code); the user types the code.
    const request = await until(() => tab.getState().pending[0], 'the pairing request in the tab')
    expect(request.label).toBe('MCP token “laptop”')
    expect(tab.accept(request.id, 'WRONG-CODE')).toBe(true)
    await until(() => tab.getState().results[request.id], 'the answer to the wrong code')
    expect(tab.getState().results[request.id]).toMatchObject({ ok: false })
    expect(tab.accept(request.id, pair.code)).toBe(true)
    await until(() => tab.getState().paired[0], 'the pairing in the tab')

    expect((await call('browser_status')).body).toMatchObject({
      attached: true,
      via: 'pairing',
      route: '/m/box',
      live_tools: ['browser_get_params', 'browser_set_param', 'browser_render'],
    })
    // Agent → socket → the tab's handler → back.
    expect((await call('browser_set_param', { name: 'width', value: 30 })).body).toEqual({ name: 'width', value: 30 })
    expect(params.width).toBe(30)
    expect((await call('browser_get_params')).body).toEqual({ width: 30 })

    // The tab's own refusals come back as errors: its schema check, its handler's own error,
    // and a tool whose page is not open.
    const bad = await call('browser_set_param', { name: 'width', value: { nested: true } })
    expect(bad.isError).toBe(true)
    const unknown = await call('browser_set_param', { name: 'depth', value: 1 })
    expect(unknown.text).toMatch(/^the tab answered set_param with failed: /)
    expect(unknown.text).toContain('No parameter \\"depth\\".')
    expect((await call('browser_get_form')).text).toMatch(/the tab answered get_form with unavailable/)

    // A page that does not answer: the call gives up (render's own wait, 0 ms here, plus the margin).
    const started = performance.now()
    const slow = await call('browser_render', { timeout_ms: 0 })
    expect(slow.text).toMatch(/the tab did not answer render within 10 s/)
    expect(performance.now() - started).toBeGreaterThanOrEqual(9_000)

    // Outward stops at the approval gate and never reaches the tab.
    expect((await call('browser_open_print_dialog')).body).toMatchObject({ status: 'pending_approval' })

    // The user disconnects the agent in the tab.
    expect(tab.end(tab.getState().paired[0]!.id)).toBe(true)
    await until(() => tab.getState().paired.length === 0 || undefined, 'the pairing to end')
    expect((await call('browser_get_params')).text).toMatch(/^no browser attached/)
    await client.close()
  }, 60_000)

  it('keeps a pairing across a dropped socket (same tab id), and says so while the tab is away', async () => {
    const { tokens } = await start()
    const tab = await openTab()
    const { token } = await tokens.mint({ name: 'laptop', tier: 'write' })
    const client = await mcpClient(token)
    const pair = firstText(await client.callTool({ name: 'browser_pair', arguments: {} })) as { code: string }
    const request = await until(() => tab.getState().pending[0], 'the pairing request')
    tab.accept(request.id, pair.code)
    await until(() => tab.getState().paired[0], 'the pairing')

    tab.close()
    await until(() => !tabs.connected().includes(TAB_ID) || undefined, 'the tab to go')
    const away = await client.callTool({ name: 'browser_get_params', arguments: {} })
    expect((away.content as { text: string }[])[0]!.text).toMatch(/the paired ScadBuddy tab is not connected/)

    // The page is still open; its socket comes back with the same id.
    await openTab()
    expect(firstText(await client.callTool({ name: 'browser_get_params', arguments: {} }))).toEqual({ width: 20 })
    await client.close()
  }, 30_000)

  it.skipIf(cliMissing !== undefined)(
    "a chat turn's browser_set_param runs in the tab the panel chats from, and the model gets the tab's answer",
    async () => {
      script = (r) => {
        const last = JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
        if (last.includes('tool_result')) return { text: 'Width is now 42.' }
        return { toolUse: { name: 'mcp__scadbuddy__browser_set_param', input: { name: 'width', value: 42 } } }
      }
      await start()
      await openTab()
      const panel = await openPanelSocket(agent!)
      const { clientMessage } = await frontendClientMessages()
      panel.send(clientMessage({ type: 'tab.bind', tabId: TAB_ID }))
      panel.send(clientMessage({ type: 'user.message', text: 'Make it 42 wide', context: { route: '/m/box' } }))
      // A new session reports idle before its first turn runs; the turn ends with its result.
      const frames = await panel.until((f) => f.type === 'session.result', { timeoutMs: 60_000 })

      expect(params.width).toBe(42)
      expect(frames.find((f) => f.type === 'tool.call')).toMatchObject({
        name: 'mcp__scadbuddy__browser_set_param',
        risk: 'write',
      })
      expect(frames.find((f) => f.type === 'tool.result')).toMatchObject({ ok: true })
      // The tab's answer is what the model read next.
      const second = JSON.stringify(fake!.messageCalls().at(-1)?.body?.messages ?? [])
      expect(second).toContain('\\"value\\": 42')
      expect(frames.filter((f) => f.type === 'assistant.text.delta').map((f) => f.delta).join('')).toBe('Width is now 42.')
      panel.close()
    },
    90_000,
  )
})
