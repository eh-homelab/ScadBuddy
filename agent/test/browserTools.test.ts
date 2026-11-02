import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { z } from 'zod'
import { type Principal, tiersUpTo, TIERS } from '../src/auth/principal.js'
import { principalFor } from '../src/auth/tokens.js'
import { type BrowserTabs, MAX_SESSION_TABS, TabHub, type TabConnection } from '../src/bridge/hub.js'
import type { PairingStore } from '../src/bridge/pairings.js'
import type { AgentFrame } from '../src/bridge/protocol.js'
import type { TabRelay } from '../src/bridge/relay.js'
import type { SessionTabStore } from '../src/bridge/sessionTabs.js'
import { MemoryEventSource } from '../src/events/bus.js'
import { ResourceHub } from '../src/resources/hub.js'
import { ChatConnection } from '../src/routes/chat.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { browserTools, STALE_TAB_HINT, tabBackNotRun } from '../src/tools/browser.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS, tierOf } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { connect, firstText, services, testApp } from './helpers/mcp.js'
import { frontendBridgeCatalog } from './support/frontendProtocol.js'
import { InMemoryPairingStore } from './support/memoryPairings.js'

// The browser_* tools (#254) without a socket: the tab is a TabConnection fed
// frames by hand (bridge/hub.ts), so each tool's forwarding, its errors
// ("no browser attached", a timeout, a dropped tab) and its tier are checked
// in both projections. test/bridge.e2e.test.ts runs the real socket and the
// tab's own link code; test/bridgePairings.pg.test.ts the Postgres store.

const TAB = 'tab-aaaaaaaaaaaaaaaaaaaaaa'
const OTHER_TAB = 'tab-bbbbbbbbbbbbbbbbbbbbbb'
const browser: Principal = { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') }

type Answer = (call: Extract<AgentFrame, { type: 'call' }>) => unknown

/** A tab on `hub`: every frame it was sent, and an answer for each call (undefined: never answers). */
async function tab(hub: TabHub, tabId = TAB, answer: Answer = () => ({ ok: true, result: { done: true } })) {
  const frames: AgentFrame[] = []
  let replaced = false
  const conn: TabConnection = hub.open(
    (frame) => {
      frames.push(frame)
      if (frame.type !== 'call') return
      const outcome = answer(frame)
      if (outcome !== undefined) {
        queueMicrotask(() => void conn.receive(JSON.stringify({ v: 1, type: 'result', id: frame.id, outcome })))
      }
    },
    () => {
      replaced = true
    },
  )
  await conn.receive(JSON.stringify({ v: 1, type: 'hello', tabId, route: '/', live: ['navigate', 'snapshot', 'search'] }))
  return {
    conn,
    frames,
    calls: () => frames.filter((f): f is Extract<AgentFrame, { type: 'call' }> => f.type === 'call'),
    replaced: () => replaced,
  }
}

function ctx(hub: BrowserTabs | undefined, principal: Principal, signal = new AbortController().signal): ToolContext {
  return { ...services(hub ? { browser: hub } : {}), principal, progress: async () => {}, signal }
}

const tool = (name: string) => ALL_TOOLS.find((t) => t.name === name)!
const text = (result: { content: unknown[] }) => (result.content[0] as { text: string }).text

describe('the browser_* tools match the tab catalogue (frontend catalog.ts)', () => {
  /** A JSON Schema with what the two sides may say differently taken out. */
  function comparable(schema: unknown): unknown {
    if (Array.isArray(schema)) return schema.map(comparable)
    if (!schema || typeof schema !== 'object') return schema
    const out: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(schema)) {
      // Descriptions are worded for each audience; the tab's objects are .strict().
      if (key === 'description' || key === '$schema' || key === 'additionalProperties' || key === 'propertyNames') continue
      out[key] = comparable(value)
    }
    // A record (the tab) and a catchall object (here, for the SDK's sake: common.ts `params`) are the same map.
    if (out.type === 'object' && out.properties && Object.keys(out.properties).length === 0) delete out.properties
    if (Array.isArray(out.type)) out.type = [...(out.type as string[])].sort()
    if (Array.isArray(out.anyOf)) out.anyOf = [...out.anyOf].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))
    return out
  }

  it('name every Settings area in navigate, as the tab does', async () => {
    const AREAS = ['general', 'printing', 'library', 'assistant', 'tools', 'mcp', 'audits', 'system']
    const { TOOLS } = await frontendBridgeCatalog()
    for (const a of AREAS) {
      expect(tool('browser_navigate').description).toContain(`"/settings/${a}"`)
      expect(TOOLS.navigate?.description).toContain(`"/settings/${a}"`)
    }
  })

  it('forward every tab tool as browser_<name>, and nothing else', async () => {
    const { TOOLS } = await frontendBridgeCatalog()
    const forwarded = browserTools.map((t) => t.name).filter((n) => n !== 'browser_status' && n !== 'browser_pair')
    expect(forwarded.sort()).toEqual(Object.keys(TOOLS).map((n) => `browser_${n}`).sort())
  })

  it('take the arguments the tab takes, with the same defaults', async () => {
    const { TOOLS } = await frontendBridgeCatalog()
    for (const [name, spec] of Object.entries(TOOLS)) {
      const ours = z.toJSONSchema(z.object(tool(`browser_${name}`).shape), { io: 'input' })
      const theirs = z.toJSONSchema(spec.input as z.ZodType, { io: 'input' })
      expect(comparable(ours), name).toEqual(comparable(theirs))
    }
  })

  it('tier each no lower than the tab does, and anything that moves or changes the tab at least write', async () => {
    const { TOOLS } = await frontendBridgeCatalog()
    for (const [name, spec] of Object.entries(TOOLS)) {
      const risk = tool(`browser_${name}`).risk
      expect(TIERS.indexOf(risk), name).toBeGreaterThanOrEqual(TIERS.indexOf(spec.risk))
    }
    expect(tool('browser_navigate').risk).toBe('write')
    expect(tool('browser_open_model').risk).toBe('write')
    // Outward in the tab too, so it stops at the approval gate (spec §8.2).
    expect(tool('browser_open_print_dialog').gated).toBe(true)
    expect(tierOf('mcp__scadbuddy__browser_click')).toBe('write')
    expect(tierOf('mcp__scadbuddy__browser_snapshot')).toBe('read')
  })
})

describe('forwarding to the tab', () => {
  it('sends the call to the tab and returns its answer', async () => {
    const hub = new TabHub()
    const t = await tab(hub, TAB, (call) => ({ ok: true, result: { route: call.args.route } }))
    hub.pairSession('s1', TAB)
    const result = await runTool(tool('browser_navigate'), { route: '/settings' }, ctx(hub.forSession('s1'), browser))
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual({ route: '/settings' })
    expect(t.calls()).toMatchObject([{ type: 'call', tool: 'navigate', args: { route: '/settings' } }])
  })

  it('passes the arguments with their defaults applied, and waits longer for a tool that waits itself', async () => {
    const hub = new TabHub({ callTimeoutMs: 20 })
    const t = await tab(hub, TAB, () => undefined)
    hub.pairSession('s1', TAB)
    let answered = false
    const pending = runTool(tool('browser_render'), {}, ctx(hub.forSession('s1'), browser)).then((r) => {
      answered = true
      return r
    })
    await new Promise((r) => setTimeout(r, 60))
    // Past the hub's 20 ms default: render's own 30 s wait plus the margin applies.
    expect(answered).toBe(false)
    expect(t.calls()[0]).toMatchObject({ tool: 'render', args: { timeout_ms: 30_000 } })
    t.conn.close()
    expect(text(await pending)).toMatch(/did not finish: the tab disconnected/)
  })

  it('says "no browser attached" when the session has no tab', async () => {
    const hub = new TabHub()
    await tab(hub)
    const result = await runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s-none'), browser))
    expect(result.isError).toBe(true)
    expect(text(result)).toMatch(/^no browser attached: no ScadBuddy tab is paired with this session/)
  })

  it('says "no browser attached" without the bridge at all', async () => {
    const result = await runTool(tool('browser_snapshot'), {}, ctx(undefined, browser))
    expect(text(result)).toMatch(/^no browser attached/)
  })

  it('says the paired tab is not connected once it has gone', async () => {
    const hub = new TabHub()
    const t = await tab(hub)
    hub.pairSession('s1', TAB)
    t.conn.close()
    const result = await runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s1'), browser))
    expect(text(result)).toMatch(/^no browser attached: the paired ScadBuddy tab is not connected/)
  })

  it('gives up after the timeout, saying the tab may still finish', async () => {
    const hub = new TabHub({ callTimeoutMs: 30 })
    const t = await tab(hub, TAB, () => undefined)
    hub.pairSession('s1', TAB)
    const result = await runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s1'), browser))
    expect(result.isError).toBe(true)
    expect(text(result)).toMatch(/the tab did not answer snapshot within 0 s; it may still finish/)
    // A late answer is dropped quietly.
    await t.conn.receive(JSON.stringify({ v: 1, type: 'result', id: t.calls()[0]!.id, outcome: { ok: true, result: 1 } }))
    expect(t.conn.calls.size).toBe(0)
  })

  it('stops waiting when the call is cancelled', async () => {
    const hub = new TabHub()
    await tab(hub, TAB, () => undefined)
    hub.pairSession('s1', TAB)
    const controller = new AbortController()
    const pending = runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s1'), browser, controller.signal))
    controller.abort()
    expect(text(await pending)).toBe('the call was cancelled')
  })

  it("wraps the tab's own error as untrusted text after ScadBuddy's summary", async () => {
    const hub = new TabHub()
    await tab(hub, TAB, () => ({
      ok: false,
      error: { code: 'invalid_args', message: 'The arguments do not match.', issues: [{ path: 'value', message: 'too big' }] },
    }))
    hub.pairSession('s1', TAB)
    const result = await runTool(tool('browser_set_param'), { name: 'w', value: 99 }, ctx(hub.forSession('s1'), browser))
    expect(result.isError).toBe(true)
    const message = text(result)
    expect(message.startsWith('the tab answered set_param with invalid_args: ')).toBe(true)
    expect(firstText({ content: [{ type: 'text', text: message.slice(message.indexOf(': ') + 2) }] })).toBe(
      'The arguments do not match. value: too big',
    )
  })

  it("gives stale_tab's guidance as ScadBuddy's own text, outside the envelope (#4884)", async () => {
    const hub = new TabHub()
    await tab(hub, TAB, () => ({ ok: false, error: { code: 'stale_tab', message: 'The tab is reloading.' } }))
    hub.pairSession('s1', TAB)
    const result = await runTool(tool('browser_set_param'), { name: 'w', value: 9 }, ctx(hub.forSession('s1'), browser))
    expect(result.isError).toBe(true)
    const message = text(result)
    const trusted = `the tab answered set_param with stale_tab (${STALE_TAB_HINT}): `
    expect(message.startsWith(trusted)).toBe(true)
    expect(STALE_TAB_HINT).toMatch(/browser_pair/)
    expect(firstText({ content: [{ type: 'text', text: message.slice(trusted.length) }] })).toBe('The tab is reloading.')
  })

  it('lets a reconnect with the same tab id take over, closing the old socket', async () => {
    const hub = new TabHub()
    const first = await tab(hub, TAB, () => undefined)
    hub.pairSession('s1', TAB)
    const stuck = runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s1'), browser))
    await new Promise((r) => setTimeout(r, 5))
    const second = await tab(hub, TAB)
    expect(first.replaced()).toBe(true)
    expect(text(await stuck)).toMatch(/did not finish: the tab connected again/)
    const result = await runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s1'), browser))
    expect(firstText(result)).toEqual({ done: true })
    expect(second.calls()).toHaveLength(1)
  })

  it('refuses frames before hello, and malformed ones', async () => {
    const hub = new TabHub()
    const frames: AgentFrame[] = []
    const conn = hub.open((f) => frames.push(f))
    await conn.receive(JSON.stringify({ v: 1, type: 'state', route: '/', live: [] }))
    await conn.receive('not json')
    await conn.receive(Buffer.from('binary'))
    await conn.receive(42)
    expect(frames.map((f) => f.type === 'error' && f.message)).toEqual([
      'send hello first',
      'ignored a malformed frame: frame is not JSON',
      'send hello first',
      'frames must be JSON text or image bytes',
    ])
    expect(hub.connected()).toEqual([])
  })

  it('reports where the tab is and what is live (browser_status)', async () => {
    const hub = new TabHub()
    const t = await tab(hub)
    hub.pairSession('s1', TAB)
    await t.conn.receive(JSON.stringify({ v: 1, type: 'state', route: '/m/box', live: ['get_params', 'set_param'] }))
    const result = await runTool(tool('browser_status'), {}, ctx(hub.forSession('s1'), browser))
    expect(firstText(result)).toEqual({
      attached: true,
      via: 'session',
      route: '/m/box',
      live_tools: ['browser_get_params', 'browser_set_param'],
    })
  })
})

describe('the harness projection', () => {
  it("reaches the tab of the turn's own session, as mcp__scadbuddy__browser_*", async () => {
    const hub = new TabHub()
    const mine = await tab(hub, TAB)
    const other = await tab(hub, OTHER_TAB)
    hub.pairSession('s1', TAB)
    hub.pairSession('s2', OTHER_TAB)
    const servers = harnessTools(services({ browser: hub })).mcpServers({
      id: 's1',
      owner: { kind: 'browser', id: 'browser', label: 'You' },
    })
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await servers.scadbuddy!.instance.connect(serverSide)
    const client = new Client({ name: 'harness-test', version: '0' })
    await client.connect(clientSide)
    const listed = (await client.listTools()).tools.map((t) => t.name)
    expect(listed).toEqual(expect.arrayContaining(['browser_snapshot', 'browser_click', 'browser_open_print_dialog']))
    const result = await client.callTool({ name: 'browser_search', arguments: { query: 'box' } })
    await client.close()
    expect(result.isError).toBeFalsy()
    expect(mine.calls()).toMatchObject([{ tool: 'search', args: { query: 'box' } }])
    expect(other.calls()).toEqual([])
  })
})

describe('the harness projection waits for the tab (#815)', () => {
  it("hands the turn's waitForTab the call's tool_use id from Claude Code's _meta", async () => {
    const hub = new TabHub()
    const seen: (string | undefined)[] = []
    const servers = harnessTools(services({ browser: hub })).mcpServers(
      { id: 's1', owner: { kind: 'browser', id: 'browser', label: 'You' } },
      undefined,
      {
        waitForTab: ({ toolUseId }) => {
          seen.push(toolUseId)
          return Promise.resolve({ back: false, message: 'nobody came back.' })
        },
      },
    )
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await servers.scadbuddy!.instance.connect(serverSide)
    const client = new Client({ name: 'harness-test', version: '0' })
    await client.connect(clientSide)
    const result = await client.callTool({ name: 'browser_snapshot', arguments: {}, _meta: { 'claudecode/toolUseId': 'toolu_meta1' } })
    await client.close()
    expect(result.isError).toBe(true)
    expect(text(result as { content: unknown[] })).toMatch(/nobody came back\.$/)
    expect(seen).toEqual(['toolu_meta1'])
  })
})

describe('the chat socket pairs the sessions it chats with (tab.bind)', () => {
  function fakeSessions() {
    const sends: string[] = []
    const sessions = {
      start: async () => ({ session: { id: 's-new' } }),
      send: async (id: string) => {
        sends.push(id)
      },
      get: async () => ({}),
      attach: async () => ({
        async *[Symbol.asyncIterator]() {},
      }),
      events: { lastSeq: async () => 0 },
    } as unknown as SessionManager
    return { sessions, sends }
  }
  const v = 1
  const context = { route: '/' }

  it('pairs a new session and one it sends to with the bound tab, and attaches only an unpaired one', async () => {
    const hub = new TabHub()
    await tab(hub, TAB)
    await tab(hub, OTHER_TAB)
    const { sessions } = fakeSessions()
    const chat = new ChatConnection(sessions, () => {}, { tabs: hub })
    await chat.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await chat.receive(JSON.stringify({ v, type: 'user.message', text: 'hi', context }))
    expect((await hub.sessionTab('s-new')).connected).toBe(true)
    expect((await hub.status({ principal: browser, sessionId: 's-new' })).attached).toBe(true)

    // Another tab's panel attaching to it does not steal it; sending from there does.
    const elsewhere = new ChatConnection(sessions, () => {}, { tabs: hub })
    await elsewhere.receive(JSON.stringify({ v, type: 'tab.bind', tabId: OTHER_TAB }))
    await elsewhere.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    const call = () => hub.call({ principal: browser, sessionId: 's-new' }, 'snapshot', {}, { signal: new AbortController().signal })
    expect(await call()).toEqual({ ok: true, result: { done: true } })
    await elsewhere.receive(JSON.stringify({ v, type: 'user.message', sessionId: 's-new', text: 'more', context }))
    const status = await hub.status({ principal: browser, sessionId: 's-new' })
    expect(status).toMatchObject({ attached: true, via: 'session' })
    chat.close()
    elsewhere.close()
  })

  it("asks the shared row, not this replica's map, whether an attached session has a tab", async () => {
    // Two replicas sharing ai_session_tabs (#2086). `here` paired s-new with TAB,
    // which is connected to it; the user then sent to s-new from OTHER_TAB, through
    // `there`, and closed that tab. `here`'s map still says TAB, so attaching from
    // TAB took the session for paired and left the row on a tab nobody holds.
    const rows = new Map<string, string>()
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
    }
    const here = new TabHub({ sessionTabs: store })
    const there = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    here.pairSession('s-new', TAB)
    there.pairSession('s-new', OTHER_TAB)
    await new Promise((r) => setTimeout(r, 0))
    expect((await here.sessionTab('s-new')).connected).toBe(false)

    const { sessions } = fakeSessions()
    const chat = new ChatConnection(sessions, () => {}, { tabs: here })
    await chat.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await chat.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await new Promise((r) => setTimeout(r, 0))
    expect(rows.get('s-new')).toBe(TAB)
    chat.close()
  })

  /** Two replicas' view of one ai_session_tabs: `rows` is the table. */
  function sharedRows() {
    const rows = new Map<string, string>()
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
    }
    return { rows, store }
  }
  const tick = () => new Promise((r) => setTimeout(r, 0))

  it("prefers this replica's pairing while its write to the shared row has failed (#2211)", async () => {
    const { rows } = sharedRows()
    let failing = true
    const logged: string[] = []
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        if (failing) throw new Error('session tab write failed')
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
    }
    const here = new TabHub({ sessionTabs: store, log: (m) => logged.push(m) })
    await tab(here, TAB)
    rows.set('s-new', OTHER_TAB) // an older pairing, on a tab nobody holds now
    here.pairSession('s-new', TAB)
    expect((await here.sessionTab('s-new')).connected).toBe(true)
    expect(await here.status({ principal: browser, sessionId: 's-new' })).toMatchObject({ attached: true, via: 'session' })
    expect(logged.join('\n')).toMatch(/session tab write failed/)

    // A later write that lands hands the answer back to the row.
    failing = false
    here.pairSession('s-new', TAB)
    await tick()
    rows.set('s-new', OTHER_TAB) // another replica paired it since
    expect((await here.sessionTab('s-new')).connected).toBe(false)
  })

  it("leaves a session alone when attached from another tab while another replica holds the session's tab", async () => {
    const { rows, store } = sharedRows()
    const relay: TabRelay = {
      serve: () => {},
      attached: () => {},
      detached: () => {},
      forward: async () => ({ op: 'status', route: '/', live: [] }),
    }
    const here = new TabHub({ sessionTabs: store, relay })
    await tab(here, TAB)
    rows.set('s-new', OTHER_TAB) // paired on the replica that holds OTHER_TAB
    expect((await here.sessionTab('s-new')).connected).toBe(true)

    const { sessions } = fakeSessions()
    const chat = new ChatConnection(sessions, () => {}, { tabs: here })
    await chat.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await chat.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await tick()
    expect(rows.get('s-new')).toBe(OTHER_TAB)
    chat.close()
  })

  it('pairs the attaching tab when the relay fails, and logs the failure', async () => {
    const { rows, store } = sharedRows()
    const logged: string[] = []
    const relay: TabRelay = {
      serve: () => {},
      attached: () => {},
      detached: () => {},
      forward: async () => {
        throw new Error('bridge insert failed')
      },
    }
    const here = new TabHub({ sessionTabs: store, relay, log: (m) => logged.push(m) })
    await tab(here, TAB)
    rows.set('s-new', OTHER_TAB)
    expect((await here.sessionTab('s-new')).connected).toBe(false)
    expect(logged.join('\n')).toMatch(/bridge insert failed/)

    const events: string[] = []
    const { sessions } = fakeSessions()
    const chat = new ChatConnection(sessions, (e) => events.push(JSON.stringify(e)), { tabs: here })
    await chat.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await chat.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await tick()
    expect(rows.get('s-new')).toBe(TAB)
    expect(events.filter((e) => e.includes('"error"'))).toEqual([])
    chat.close()
  })

  it("does not hold the socket's next message while it asks whether an attached session has a tab", async () => {
    const pairs: string[] = []
    const tabs = {
      pairSession: (sessionId: string, tabId: string) => void pairs.push(`${sessionId}:${tabId}`),
      sessionTab: () => new Promise<{ tabId: string | undefined; connected: boolean }>(() => {}), // a relay that never answers
      pairSessionIfStill: async () => false,
    }
    const { sessions, sends } = fakeSessions()
    const chat = new ChatConnection(sessions, () => {}, { tabs })
    await chat.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    const handled = (async () => {
      await chat.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's1' }))
      await chat.receive(JSON.stringify({ v, type: 'user.message', sessionId: 's1', text: 'hi', context }))
      return 'handled'
    })()
    const waited = new Promise((r) => setTimeout(() => r('held'), 1_000))
    expect(await Promise.race([handled, waited])).toBe('handled')
    expect(sends).toEqual(['s1'])
    expect(pairs).toEqual([`s1:${TAB}`])
    chat.close()
  })

  it("does not overwrite a pairing another socket made while the attach's check was out (#2271)", async () => {
    const { rows } = sharedRows()
    let release!: (tabId: string | undefined) => void
    let reads = 0
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      // The attach's read is held; every later one reads the table.
      get: async (sessionId) =>
        ++reads === 1 ? new Promise<string | undefined>((resolve) => (release = resolve)) : rows.get(sessionId),
    }
    // Two replicas sharing ai_session_tabs: A's socket on `here`, B's on `there`.
    const here = new TabHub({ sessionTabs: store })
    const there = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    await tab(there, OTHER_TAB)
    const { sessions } = fakeSessions()
    const a = new ChatConnection(sessions, () => {}, { tabs: here })
    const b = new ChatConnection(sessions, () => {}, { tabs: there })
    await a.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await b.receive(JSON.stringify({ v, type: 'tab.bind', tabId: OTHER_TAB }))

    await a.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await tick()
    await b.receive(JSON.stringify({ v, type: 'user.message', sessionId: 's-new', text: 'hi', context }))
    await tick()
    expect(rows.get('s-new')).toBe(OTHER_TAB)
    release(undefined) // A's check: no tab, as it was when A asked
    await tick()
    await tick()
    expect(rows.get('s-new')).toBe(OTHER_TAB)
    a.close()
    b.close()
  })

  it('compares and pairs in one write where the store can, so no pairing lands between (#2271 review)', async () => {
    const { rows } = sharedRows()
    const held: ((tabId: string | undefined) => void)[] = []
    let reads = 0
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      // The attach's reads are held: the first is its check; a second would be a
      // re-read before writing, the gap a compare-and-set closes.
      get: async (sessionId) =>
        ++reads <= 2 ? new Promise<string | undefined>((resolve) => held.push(resolve)) : rows.get(sessionId),
      setIf: async (sessionId, tabId, seen) => {
        if (rows.get(sessionId) !== seen) return false
        rows.set(sessionId, tabId)
        return true
      },
    }
    const here = new TabHub({ sessionTabs: store })
    const there = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    await tab(there, OTHER_TAB)
    const { sessions } = fakeSessions()
    const a = new ChatConnection(sessions, () => {}, { tabs: here })
    const b = new ChatConnection(sessions, () => {}, { tabs: there })
    await a.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await b.receive(JSON.stringify({ v, type: 'tab.bind', tabId: OTHER_TAB }))

    await a.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await tick()
    held.shift()!(undefined) // A's check: no tab yet
    await tick()
    // B pairs while A is between its check and its write.
    await b.receive(JSON.stringify({ v, type: 'user.message', sessionId: 's-new', text: 'hi', context }))
    await tick()
    held.shift()?.(undefined) // a stale re-read, if A made one
    await tick()
    await tick()
    expect(rows.get('s-new')).toBe(OTHER_TAB)
    a.close()
    b.close()
  })

  it("pairs neither the row nor its own map when the store declines the compare-and-set (#3478 review)", async () => {
    const { rows } = sharedRows()
    let release!: (tabId: string | undefined) => void
    let reads = 0
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      // The attach's check is held; every later read reads the table.
      get: async (sessionId) =>
        ++reads === 1 ? new Promise<string | undefined>((resolve) => (release = resolve)) : rows.get(sessionId),
      setIf: async (sessionId, tabId, seen) => {
        if (rows.has(sessionId) && rows.get(sessionId) !== seen) return false
        rows.set(sessionId, tabId)
        return true
      },
    }
    const here = new TabHub({ sessionTabs: store })
    const there = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    await tab(there, OTHER_TAB)
    const { sessions } = fakeSessions()
    const a = new ChatConnection(sessions, () => {}, { tabs: here })
    const b = new ChatConnection(sessions, () => {}, { tabs: there })
    await a.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await b.receive(JSON.stringify({ v, type: 'tab.bind', tabId: OTHER_TAB }))

    await a.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await tick()
    // B pairs while A's check is out, so A's answer is stale when it comes.
    await b.receive(JSON.stringify({ v, type: 'user.message', sessionId: 's-new', text: 'hi', context }))
    await tick()
    release(undefined)
    await tick()
    await tick()
    expect(rows.get('s-new')).toBe(OTHER_TAB)
    expect(reads).toBe(1) // no re-read: the store compared
    // With the row gone, A's answer is its own map's: A never paired it.
    rows.delete('s-new')
    expect((await here.sessionTab('s-new')).tabId).toBeUndefined()
    a.close()
    b.close()
  })

  it("keeps a pairing this replica made while its compare-and-set was out (#3478 review)", async () => {
    const { rows } = sharedRows()
    let release!: () => void
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
      setIf: async (sessionId, tabId, seen) => {
        await new Promise<void>((resolve) => (release = resolve))
        if (rows.has(sessionId) && rows.get(sessionId) !== seen) return false
        rows.set(sessionId, tabId)
        return true
      },
    }
    const here = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    await tab(here, OTHER_TAB)
    const paired = here.pairSessionIfStill('s-new', TAB, undefined)
    await tick()
    here.pairSession('s-new', OTHER_TAB) // a send from another tab, while the write is out
    release()
    expect(await paired).toBe(true)
    await tick()
    expect(rows.get('s-new')).toBe(OTHER_TAB) // its write was queued after the compare-and-set
    rows.delete('s-new')
    expect((await here.sessionTab('s-new')).tabId).toBe(OTHER_TAB)
  })

  it('keeps a re-pairing to the same tab made while its compare-and-set was out (#3787 review)', async () => {
    const { rows } = sharedRows()
    let release!: () => void
    const writes: string[] = []
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        writes.push(`set ${tabId}`)
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
      setIf: async (sessionId, tabId, seen) => {
        await new Promise<void>((resolve) => (release = resolve))
        if (rows.has(sessionId) && rows.get(sessionId) !== seen) return false
        writes.push(`setIf ${tabId}`)
        rows.set(sessionId, tabId)
        return true
      },
    }
    const here = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    await tab(here, OTHER_TAB)
    here.pairSession('s-aba', OTHER_TAB)
    await tick()
    const paired = here.pairSessionIfStill('s-aba', TAB, OTHER_TAB)
    await tick()
    here.pairSession('s-aba', OTHER_TAB) // the same tab again, while the write is out
    release()
    expect(await paired).toBe(true)
    await tick()
    expect(rows.get('s-aba')).toBe(OTHER_TAB) // its write was queued after the compare-and-set
    // The order the map's choice rests on: the compare-and-set landed, then the re-pairing's write (#3825 review).
    expect(writes).toEqual([`set ${OTHER_TAB}`, `setIf ${TAB}`, `set ${OTHER_TAB}`])
    rows.delete('s-aba')
    expect((await here.sessionTab('s-aba')).tabId).toBe(OTHER_TAB)
  })

  it('pairs a session evicted while its compare-and-set was out once the write lands (#3825 review)', async () => {
    const { rows } = sharedRows()
    let release!: () => void
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
      setIf: async (sessionId, tabId, seen) => {
        if (sessionId === 's-evicted') await new Promise<void>((resolve) => (release = resolve))
        if (rows.has(sessionId) && rows.get(sessionId) !== seen) return false
        rows.set(sessionId, tabId)
        return true
      },
    }
    const here = new TabHub({ sessionTabs: store })
    await tab(here, TAB)
    await tab(here, OTHER_TAB)
    here.pairSession('s-evicted', OTHER_TAB)
    await tick()
    const paired = here.pairSessionIfStill('s-evicted', TAB, OTHER_TAB)
    await tick()
    // Enough other sessions paired here to evict it from the map while the write is out.
    for (let i = 0; i < MAX_SESSION_TABS; i++) here.pairSession(`s-other-${i}`, OTHER_TAB)
    release()
    expect(await paired).toBe(true)
    await tick()
    expect(rows.get('s-evicted')).toBe(TAB)
    rows.delete('s-evicted')
    expect((await here.sessionTab('s-evicted')).tabId).toBe(TAB)
  })

  it('pairs only here when the compare-and-set fails, and queues no write that could land later (#3478 review)', async () => {
    const { rows } = sharedRows()
    const sets: string[] = []
    const logged: string[] = []
    const store: SessionTabStore = {
      set: async (sessionId, tabId) => {
        sets.push(`${sessionId}:${tabId}`)
        rows.set(sessionId, tabId)
      },
      get: async (sessionId) => rows.get(sessionId),
      setIf: async () => {
        throw new Error('session tab write failed')
      },
    }
    const here = new TabHub({ sessionTabs: store, log: (m) => logged.push(m) })
    await tab(here, TAB)
    const { sessions } = fakeSessions()
    const a = new ChatConnection(sessions, () => {}, { tabs: here })
    await a.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await a.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's-new' }))
    await tick()
    await tick()
    expect(logged.join('\n')).toMatch(/session tab write failed/)
    expect(sets).toEqual([])
    expect(rows.has('s-new')).toBe(false)
    expect(await here.sessionTab('s-new')).toEqual({ tabId: TAB, connected: true })
    a.close()
  })

  it('pairs nothing when the socket closes while the check is out (#2271)', async () => {
    let answer!: (value: { tabId: string | undefined; connected: boolean }) => void
    const paired: string[] = []
    const tabs = {
      pairSession: (sessionId: string, tabId: string) => void paired.push(`${sessionId}:${tabId}`),
      sessionTab: () => new Promise<{ tabId: string | undefined; connected: boolean }>((resolve) => (answer = resolve)),
      pairSessionIfStill: async (sessionId: string, tabId: string) => {
        paired.push(`${sessionId}:${tabId}`)
        return true
      },
    }
    const { sessions } = fakeSessions()
    const chat = new ChatConnection(sessions, () => {}, { tabs })
    await chat.receive(JSON.stringify({ v, type: 'tab.bind', tabId: TAB }))
    await chat.receive(JSON.stringify({ v, type: 'session.attach', sessionId: 's1' }))
    chat.close()
    answer({ tabId: undefined, connected: false })
    await tick()
    expect(paired).toEqual([])
  })

  it('pairs nothing before the panel names its tab', async () => {
    const hub = new TabHub()
    await tab(hub, TAB)
    const { sessions } = fakeSessions()
    const chat = new ChatConnection(sessions, () => {}, { tabs: hub })
    await chat.receive(JSON.stringify({ v, type: 'user.message', text: 'hi', context }))
    expect((await hub.sessionTab('s-new')).connected).toBe(false)
    chat.close()
  })
})

describe('/mcp callers pair by code (spec §8.5)', () => {
  const closers: (() => Promise<void>)[] = []
  afterEach(async () => {
    await Promise.all(closers.splice(0).map((close) => close()))
  })

  async function setup(tier: 'read' | 'write' | 'outward' = 'outward') {
    const pairings = new InMemoryPairingStore()
    const hub = new TabHub({ pairings })
    const t = await tab(hub, TAB, (call) => ({ ok: true, result: { tool: call.tool } }))
    const { app, tokens } = testApp({
      services: services({ browser: hub }),
      mcp: { resources: new ResourceHub(new MemoryEventSource()) },
    })
    const { token, record } = await tokens.mint({ name: 'laptop', tier })
    const principal = principalFor(record.id, tier)
    const client = await connect(app, { headers: { authorization: `Bearer ${token}` } })
    closers.push(() => client.close())
    const callTool = async (name: string, args: Record<string, unknown> = {}) => {
      const result = (await client.callTool({ name, arguments: args })) as { isError?: boolean; content: unknown[] }
      return { isError: result.isError ?? false, body: firstText(result), text: text(result) }
    }
    return { hub, t, pairings, principal, callTool, client }
  }

  const pairingsFrame = (t: Awaited<ReturnType<typeof tab>>) =>
    t.frames.filter((f): f is Extract<AgentFrame, { type: 'pairings' }> => f.type === 'pairings').at(-1)

  it('pairs only once the user types the code into the tab, and then drives that tab', async () => {
    const { t, callTool } = await setup()
    expect((await callTool('browser_status')).body).toMatchObject({ attached: false })
    expect((await callTool('browser_snapshot')).text).toMatch(/^no browser attached: no ScadBuddy tab is paired with this caller/)

    const pair = (await callTool('browser_pair')).body as { status: string; code: string }
    expect(pair).toMatchObject({ status: 'pending', code: expect.stringMatching(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/) })
    // The tab is told a request is waiting, never the code.
    await expect.poll(() => pairingsFrame(t)?.pending.length).toBe(1)
    const request = pairingsFrame(t)!.pending[0]!
    expect(JSON.stringify(t.frames)).not.toContain(pair.code)

    await t.conn.receive(JSON.stringify({ v: 1, type: 'pairing.accept', id: request.id, code: 'AAAA-AAAA' }))
    expect(t.frames.filter((f) => f.type === 'pairing.result').at(-1)).toMatchObject({
      ok: false,
      message: expect.stringMatching(/4 tries left/),
    })
    expect((await callTool('browser_status')).body).toMatchObject({ attached: false })

    await t.conn.receive(JSON.stringify({ v: 1, type: 'pairing.accept', id: request.id, code: pair.code.toLowerCase() }))
    expect(pairingsFrame(t)).toMatchObject({ pending: [], paired: [{ id: request.id }] })
    expect((await callTool('browser_status')).body).toMatchObject({ attached: true, via: 'pairing', route: '/' })
    expect((await callTool('browser_snapshot')).body).toEqual({ tool: 'snapshot' })
    expect((await callTool('browser_pair')).body).toMatchObject({ status: 'paired' })

    // The user disconnects it in the tab.
    await t.conn.receive(JSON.stringify({ v: 1, type: 'pairing.end', id: request.id }))
    expect((await callTool('browser_snapshot')).text).toMatch(/^no browser attached/)
  })

  it('does nothing on a denied request', async () => {
    const { t, callTool } = await setup()
    await callTool('browser_pair')
    await expect.poll(() => pairingsFrame(t)?.pending.length).toBe(1)
    await t.conn.receive(JSON.stringify({ v: 1, type: 'pairing.deny', id: pairingsFrame(t)!.pending[0]!.id }))
    expect(pairingsFrame(t)?.pending).toEqual([])
    expect((await callTool('browser_status')).body).toMatchObject({ attached: false })
  })

  it('gates the outward tool: a pending approval, and nothing reaches the tab', async () => {
    const { t, pairings, principal, callTool } = await setup()
    const request = await pairings.request(principal)
    await pairings.accept(request.id, request.code, TAB)
    expect((await callTool('browser_status')).body).toMatchObject({ attached: true })
    const result = await callTool('browser_open_print_dialog', { kind: 'send' })
    expect(result.body).toMatchObject({ status: 'pending_approval', summary: 'Open the Send to Bambuddy dialog in your ScadBuddy tab' })
    expect(t.calls()).toEqual([])
  })

  it("refuses a tool above the token's tier before it reaches the tab", async () => {
    const { t, callTool } = await setup('read')
    expect((await callTool('browser_pair')).text).toMatch(/needs the "write" tier/)
    expect((await callTool('browser_click', { role: 'button', name: 'Render' })).text).toMatch(/needs the "write" tier/)
    expect(t.calls()).toEqual([])
  })

  it('serves the paired tab\'s snapshot as scadbuddy://browser/paired/snapshot, and no other tab (#254)', async () => {
    const { t, pairings, principal, client } = await setup('read')
    const uri = 'scadbuddy://browser/paired/snapshot'
    // Before pairing: the tool's own "no browser attached", as an error.
    await expect(client.readResource({ uri })).rejects.toThrow(/no browser attached/)
    await expect(client.subscribeResource({ uri })).rejects.toThrow(/Resource not found/)
    const request = await pairings.request(principal)
    await pairings.accept(request.id, request.code, TAB)
    const { contents } = await client.readResource({ uri })
    expect(contents[0]).toMatchObject({ uri, mimeType: 'application/json' })
    expect(JSON.parse(unwrapUntrusted((contents[0] as { text: string }).text))).toEqual({ tool: 'snapshot' })
    expect(t.calls()).toMatchObject([{ tool: 'snapshot', args: {} }])
    // A tab id (or anything else) names no resource: the caller is never shown one.
    await expect(client.readResource({ uri: `scadbuddy://browser/${TAB}/snapshot` })).rejects.toMatchObject({ code: -32002 })
    await expect(client.subscribeResource({ uri: `scadbuddy://browser/${TAB}/snapshot` })).rejects.toMatchObject({ code: -32002 })
    expect(t.calls()).toHaveLength(1)
    await client.subscribeResource({ uri })
    const completion = await client.complete({
      ref: { type: 'ref/resource', uri: 'scadbuddy://browser/{tab}/snapshot' },
      argument: { name: 'tab', value: '' },
    })
    expect(completion.completion.values).toEqual(['paired'])
  })

  it('returns a screenshot over /mcp as image content (#254)', async () => {
    const pairings = new InMemoryPairingStore()
    const hub = new TabHub({ pairings })
    const bytes = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from('pixels')])
    const frames: AgentFrame[] = []
    const conn: TabConnection = hub.open((frame) => {
      frames.push(frame)
      if (frame.type !== 'call') return
      queueMicrotask(() => {
        void conn.receive(JSON.stringify({ v: 1, type: 'image', id: frame.id, mediaType: 'image/png', size: bytes.length }))
        void conn.receive(bytes)
        const result = { image: { mediaType: 'image/png' }, width: 2, height: 1, hidden: 0 }
        void conn.receive(JSON.stringify({ v: 1, type: 'result', id: frame.id, outcome: { ok: true, result } }))
      })
    })
    await conn.receive(JSON.stringify({ v: 1, type: 'hello', tabId: TAB, route: '/', live: ['screenshot'] }))
    const { app, tokens } = testApp({ services: services({ browser: hub }) })
    const { token, record } = await tokens.mint({ name: 'laptop', tier: 'read' })
    const request = await pairings.request(principalFor(record.id, 'read'))
    await pairings.accept(request.id, request.code, TAB)
    const client = await connect(app, { headers: { authorization: `Bearer ${token}` } })
    closers.push(() => client.close())
    const result = (await client.callTool({ name: 'browser_screenshot', arguments: {} })) as {
      isError?: boolean
      content: { type: string; data?: string; mimeType?: string; text?: string }[]
    }
    expect(result.isError ?? false).toBe(false)
    const image = result.content.find((c) => c.type === 'image')!
    expect(image.mimeType).toBe('image/png')
    expect(Buffer.from(image.data!, 'base64').equals(bytes)).toBe(true)
    // After the envelope's preamble and the image, the rest of the answer as JSON.
    const texts = result.content.filter((c) => c.type === 'text')
    expect(JSON.parse(unwrapUntrusted(texts.at(-1)!.text!))).toEqual({ width: 2, height: 1, hidden: 0 })
    expect(frames.filter((f) => f.type === 'error')).toEqual([])
  })

  it('lets a read-tier token point at a control: browser_highlight reaches the tab ungated (#1919)', async () => {
    const { t, pairings, principal, callTool } = await setup('read')
    const request = await pairings.request(principal)
    await pairings.accept(request.id, request.code, TAB)
    const result = await callTool('browser_highlight', { role: 'button', name: 'Render' })
    expect(result.isError).toBe(false)
    expect(result.body).toEqual({ tool: 'highlight' })
    expect(t.calls()).toMatchObject([{ type: 'call', tool: 'highlight', args: { role: 'button', name: 'Render' } }])
  })
})

// #815 §2: a call that finds no tab, in a session the user owns, waits for the
// tab as an attention request (sessions/manager.ts `waitForTab`) and runs once
// more when it is back. The Postgres side is test/attention.pg.test.ts.
describe('waiting for the tab (#815)', () => {
  it('tells the hub listener when a session has a connected tab again: a reconnect, or a pairing to a live tab', async () => {
    const hub = new TabHub()
    const back: string[] = []
    hub.onSessionTab = (sessionId) => {
      back.push(sessionId)
      return Promise.resolve()
    }
    hub.pairSession('s1', TAB)
    expect(back).toEqual([])
    const first = await tab(hub)
    expect(back).toEqual(['s1'])
    first.conn.close()
    await tab(hub, OTHER_TAB)
    hub.pairSession('s2', OTHER_TAB)
    expect(back).toEqual(['s1', 's2'])
  })

  it('logs a listener that fails, and pairs anyway', async () => {
    const logged: string[] = []
    const hub = new TabHub({ log: (m) => logged.push(m) })
    hub.onSessionTab = () => Promise.reject(new Error('db down'))
    await tab(hub)
    hub.pairSession('s1', TAB)
    await new Promise((r) => setTimeout(r, 0))
    expect(logged.join('\n')).toMatch(/db down/)
    expect((await hub.sessionTab('s1')).connected).toBe(true)
  })

  it('waits on no tab, then runs a read call once the tab is back', async () => {
    expect(tool('browser_snapshot').risk).toBe('read')
    const hub = new TabHub()
    const waits: { tool: string; toolUseId: string | undefined }[] = []
    const c: ToolContext = {
      ...ctx(hub.forSession('s1'), browser),
      toolUseId: 'toolu_b1',
      waitForTab: async ({ tool, toolUseId }) => {
        waits.push({ tool, toolUseId })
        await tab(hub, TAB, () => ({ ok: true, result: { snapped: true } }))
        hub.pairSession('s1', TAB)
        return { back: true, why: 'reconnected' as const }
      },
    }
    const result = await runTool(tool('browser_snapshot'), {}, c)
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual({ snapped: true })
    expect(waits).toEqual([{ tool: 'browser_snapshot', toolUseId: 'toolu_b1' }])
  })

  // A write or outward call is never re-run on a page that may have reloaded; the model re-checks and calls again.
  it.each([
    ['write', 'browser_set_param', { name: 'width', value: 10 }],
    ['outward', 'browser_open_print_dialog', {}],
  ] as const)('a %s call is not re-run when the tab is back: it says so and does nothing', async (tier, name, args) => {
    expect(tool(name).risk).toBe(tier)
    const hub = new TabHub()
    let back: Awaited<ReturnType<typeof tab>> | undefined
    const result = await runTool(tool(name), args, {
      ...ctx(hub.forSession('s1'), browser),
      gate: 'harness',
      waitForTab: async () => {
        back = await tab(hub)
        hub.pairSession('s1', TAB)
        return { back: true, why: 'reconnected' as const }
      },
    })
    expect(result.isError).toBe(true)
    expect(text(result)).toBe(tabBackNotRun(name, 'reconnected'))
    expect(back!.calls()).toEqual([])
  })

  // #1393: reconnected() runs on whichever replica saw the tab, so a write call is not told to try again when
  // this replica still has none: that retry could only open a wait that never ends reconnected here.
  it.each([
    ['write', 'browser_set_param', { name: 'width', value: 10 }],
    ['outward', 'browser_open_print_dialog', {}],
  ] as const)('a %s call whose wait ended reconnected on another replica says the tab is not here, not "call again"', async (_tier, name, args) => {
    const hub = new TabHub()
    const result = await runTool(tool(name), args, {
      ...ctx(hub.forSession('s1'), browser),
      gate: 'harness',
      waitForTab: () => Promise.resolve({ back: true, why: 'reconnected' as const }),
    })
    expect(result.isError).toBe(true)
    expect(text(result)).toMatch(
      /^no browser attached: no ScadBuddy tab is paired with this session.*The session's tab reconnected, but it cannot be reached now: it may have dropped again\.$/s,
    )
    expect(text(result)).not.toContain(`call ${name} again`)
  })

  // #1410: words the reason the hub gives, not a cross-replica cause it cannot know.
  it('a write call whose wait ended reconnected names the reason no tab is here', async () => {
    const hub = new TabHub()
    // The session's tab is known but not connected here: it dropped again, or is on another replica.
    hub.pairSession('s1', TAB)
    const result = await runTool(tool('browser_set_param'), { name: 'width', value: 10 }, {
      ...ctx(hub.forSession('s1'), browser),
      gate: 'harness',
      waitForTab: () => Promise.resolve({ back: true, why: 'reconnected' as const }),
    })
    expect(text(result)).toMatch(/^no browser attached: the paired ScadBuddy tab is not connected .*it cannot be reached now/s)
  })

  // #1410: a re-check that fails keeps the not-run guidance instead of an unexpected error.
  it.each([
    ['write', 'browser_set_param', { name: 'width', value: 10 }],
    ['outward', 'browser_open_print_dialog', {}],
  ] as const)('a %s call whose re-check fails still says it was not run', async (_tier, name, args) => {
    const warned = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    onTestFinished(() => warned.mockRestore())
    const pairings: PairingStore = new InMemoryPairingStore()
    let lookups = 0
    const failure = new Error('connect ECONNREFUSED postgres://user:secret@db')
    failure.name = 'PoolError'
    pairings.pairedTab = () => (++lookups === 1 ? Promise.resolve(undefined) : Promise.reject(failure))
    const hub = new TabHub({ pairings })
    const agent: Principal = { id: 'tok-1', kind: 'bearer', tiers: tiersUpTo('outward') }
    const result = await runTool(tool(name), args, {
      ...ctx(hub.forSession('s1'), agent),
      gate: 'harness',
      waitForTab: () => Promise.resolve({ back: true, why: 'reconnected' as const }),
    })
    expect(lookups).toBe(2)
    expect(result.isError).toBe(true)
    expect(text(result)).toBe(tabBackNotRun(name, 'reconnected'))
    // #1814: logged, by the error's name only, never its message.
    expect(warned).toHaveBeenCalledWith('browser tools: re-checking the tab after its wait failed (PoolError)')
    expect(warned.mock.calls.flat().join(' ')).not.toMatch(/secret/)
  })

  // #1814: status() rejects an aborted lookup as call() does, not with the signal's raw reason.
  it('rejects a status check whose lookup is aborted with the call-cancelled error', async () => {
    const { pairings } = hangingLookups()
    pairings.pairedTab = (_principal, signal) =>
      new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(signal.reason), { once: true }))
    const hub = new TabHub({ pairings })
    const agent: Principal = { id: 'tok-1', kind: 'bearer', tiers: tiersUpTo('outward') }
    const stop = new AbortController()
    const status = hub.status({ principal: agent }, { signal: stop.signal })
    stop.abort(new Error('the raw reason'))
    await expect(status).rejects.toMatchObject({ name: 'AbortError', message: 'the call was cancelled' })
  })

  /** A pairing lookup that answers the first call and then hangs until its signal aborts. */
  function hangingLookups(): { pairings: PairingStore; seen: (AbortSignal | undefined)[] } {
    const seen: (AbortSignal | undefined)[] = []
    const pairings: PairingStore = new InMemoryPairingStore()
    pairings.pairedTab = (_principal, signal) => {
      seen.push(signal)
      if (seen.length === 1) return Promise.resolve(undefined)
      return new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(signal.reason), { once: true }))
    }
    return { pairings, seen }
  }

  // #1394, #1410: the tab check's signal reaches the pairing lookup, and its abort ends a hung one.
  it("hands the tab check's signal to the pairing lookup, so a hung lookup is cancelled", async () => {
    const { pairings, seen } = hangingLookups()
    const hub = new TabHub({ pairings })
    const agent: Principal = { id: 'tok-1', kind: 'bearer', tiers: tiersUpTo('outward') }
    const parked = new AbortController()
    let check: Promise<boolean> | undefined
    await runTool(tool('browser_snapshot'), {}, {
      ...ctx(hub.forSession('s1'), agent),
      waitForTab: ({ isBack }) => {
        check = isBack(parked.signal)
        return Promise.resolve({ back: false, message: 'x' })
      },
    })
    expect(seen[1]).toBe(parked.signal)
    parked.abort(new Error('the wait ended'))
    // Without the signal the lookup would never settle and this would time out. It
    // rejects as an aborted call does (#1814), whatever the signal's reason.
    await expect(check).rejects.toThrow('the call was cancelled')
  })

  // #1410: TabHub.call() gives its own signal to the pairing lookup behind every browser_* call.
  it('cancels a browser_* call whose pairing lookup hangs', async () => {
    const { pairings, seen } = hangingLookups()
    seen.push(undefined) // no first answer: the call's own lookup hangs
    const hub = new TabHub({ pairings })
    const agent: Principal = { id: 'tok-1', kind: 'bearer', tiers: tiersUpTo('outward') }
    const stop = new AbortController()
    const result = runTool(tool('browser_snapshot'), {}, ctx(hub.forSession('s1'), agent, stop.signal))
    await expect.poll(() => seen.length).toBe(2)
    // The call's signal, joined with its deadline (registry.ts withDeadline, #1918).
    expect(seen[1]?.aborted).toBe(false)
    stop.abort()
    expect(seen[1]?.aborted).toBe(true)
    expect(text(await result)).toMatch(/the call was cancelled/)
  })

  it("words the not-run error by why the wait ended: the user's word is not a connected tab", async () => {
    const hub = new TabHub()
    const result = await runTool(tool('browser_set_param'), { name: 'width', value: 10 }, {
      ...ctx(hub.forSession('s1'), browser),
      gate: 'harness',
      waitForTab: () => Promise.resolve({ back: true, why: 'user_back' as const }),
    })
    expect(text(result)).toBe(tabBackNotRun('browser_set_param', 'user_back'))
    expect(text(result)).toMatch(/^the user said they are back \(a tab may not be attached yet\), but browser_set_param was not run/)
    expect(tabBackNotRun('browser_click', 'reconnected')).toMatch(/^the session has a connected tab again, but browser_click was not run/)
    const read = await runTool(tool('browser_snapshot'), {}, {
      ...ctx(hub.forSession('s1'), browser),
      waitForTab: () => Promise.resolve({ back: true, why: 'user_back' as const }),
    })
    expect(text(read)).toMatch(/The user said they were back, but no tab is attached here yet\.$/)
  })

  it('fails with the wait\'s outcome when the tab did not come back, and retries only once', async () => {
    const hub = new TabHub()
    let waited = 0
    const timedOut = await runTool(tool('browser_snapshot'), {}, {
      ...ctx(hub.forSession('s1'), browser),
      waitForTab: () => {
        waited += 1
        return Promise.resolve({ back: false, message: 'timed_out: the user did not reply within 300 s.' })
      },
    })
    expect(text(timedOut)).toMatch(/^no browser attached: .*timed_out: the user did not reply within 300 s\.$/s)
    // "Back" on a replica the tab is not on: the second call fails as usual, with no second wait.
    const stillGone = await runTool(tool('browser_snapshot'), {}, {
      ...ctx(hub.forSession('s1'), browser),
      waitForTab: () => {
        waited += 1
        return Promise.resolve({ back: true, why: 'reconnected' as const })
      },
    })
    expect(text(stillGone)).toMatch(/^no browser attached: no ScadBuddy tab is paired with this session.*The session's tab reconnected, but it cannot be reached now/s)
    expect(waited).toBe(2)
  })

  it('only no_browser waits: a tab that does not answer fails as before', async () => {
    const hub = new TabHub({ callTimeoutMs: 20 })
    await tab(hub, TAB, () => undefined)
    hub.pairSession('s1', TAB)
    let waited = false
    const result = await runTool(tool('browser_snapshot'), {}, {
      ...ctx(hub.forSession('s1'), browser),
      waitForTab: () => {
        waited = true
        return Promise.resolve({ back: true, why: 'reconnected' as const })
      },
    })
    expect(text(result)).toMatch(/did not answer/)
    expect(waited).toBe(false)
  })
})
