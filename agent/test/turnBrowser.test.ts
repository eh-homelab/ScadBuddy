import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { HeadlessBrowserOptions, HeadlessBrowserPlugin } from '../src/harness/headlessBrowser.js'
import type { UiReach, UiReachMap } from '../src/harness/browserReach.js'
import type { BuiltInPolicy } from '../src/plugins/builtInTools.js'
import type { Owner } from '../src/sessions/protocol.js'
import { BROWSER_SERVER, BrowserHost, type Hosted, turnBrowserServers } from '../src/temporal/turnBrowser.js'
import {
  connectInProcess,
  TURN_TOOL_TIER_ACTIVITY,
  TURN_TOOLS_ACTIVITY,
  turnToolActivities,
  turnToolActivity,
  type TurnToolsResult,
} from '../src/temporal/turnTools.js'

// The headless browser's turn servers in a durable session (#2320), against a stand-in
// browser server: the same deny list, tiers and origin rules as a classic turn's seam.

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const BACKEND = 'http://scadbuddy.test:8080'

function fakeBrowser(calls: string[]): () => McpServer {
  return () => {
    const server = new McpServer({ name: 'playwright', version: '1' })
    for (const name of ['browser_navigate', 'browser_snapshot', 'browser_click', 'browser_evaluate', 'browser_file_upload']) {
      server.registerTool(name, { description: name, inputSchema: { url: z.string().optional(), filename: z.string().optional() } }, async (args) => {
        calls.push(`${name} ${JSON.stringify(args)}`)
        return { content: [{ type: 'text', text: `${name} ok` }] }
      })
    }
    return server
  }
}

function harness(options: { enabled?: boolean; disabled?: string[]; probe?: () => Promise<UiReachMap> } = {}) {
  const calls: string[] = []
  const approvedOrigins: string[] = []
  const remembered: unknown[][] = []
  // Only rememberDurableOrigin and loadApprovedOrigins reach it here.
  const sql = ((strings: TemplateStringsArray, ...values: unknown[]) => {
    if (strings.join('').includes('INSERT INTO ai_browser_origins')) {
      remembered.push(values)
      return Promise.resolve([{ origin: values[0] }])
    }
    return Promise.resolve(approvedOrigins.map((origin) => ({ origin })))
  }) as never
  let started = 0
  let released = 0
  const held: string[] = []
  const host = {
    browser: async (_session: string, opts: HeadlessBrowserOptions): Promise<Hosted> => {
      started++
      held.push(JSON.stringify(opts.uiReach ?? null))
      const plugin = { approve: (o: string) => void approvedOrigins.push(o) } as unknown as HeadlessBrowserPlugin
      return { client: await connectInProcess(fakeBrowser(calls)), plugin, key: '', lastUsed: 0, inFlight: 1 }
    },
    release: (h: Hosted) => {
      h.inFlight--
      released++
    },
  }
  const policy: BuiltInPolicy = {
    tierOf: (_name, tier) => tier,
    disallowed: [],
    isDisabled: (_set, tool) => (options.disabled ?? []).includes(tool),
  }
  const servers = turnBrowserServers({
    sql,
    paths: { stateDir: '/tmp/turn-browser-test' },
    host,
    headless: { backendUrl: BACKEND, browserAllowedOrigins: 'https://makerworld.com', ...(options.probe ? { probe: options.probe } : {}) },
    settings: { get: async <T,>(key: string) => (key === 'headless_browser_enabled' ? (options.enabled ?? true) : undefined) as T },
    builtIn: async () => policy,
  })
  const deps = {
    sessions: { ownerOf: async (id: string) => (id === SESSION ? BROWSER : undefined) },
    servers: async (context: Parameters<typeof servers>[0]) => ({ servers: await servers(context), problems: [], secrets: [], release: () => {} }),
    approvals: { approved: async (requestId: string) => requestId.endsWith(':toolu_approved') },
  }
  return { calls, remembered, approvedOrigins, held, started: () => started, released: () => released, activities: turnToolActivities(deps), call: turnToolActivity(deps) }
}

function env(activityType: string, activityId = 'tool-toolu_01') {
  return new MockActivityEnvironment({
    activityId,
    activityType,
    workflowExecution: { workflowId: `session-${SESSION}`, runId: 'run-1' },
    taskQueue: 'agent-tools',
  })
}

const NAV = `${BROWSER_SERVER}__browser_navigate`

describe('the headless browser in a durable turn', () => {
  it('offers its tools under their classic names, minus the deny list and what Settings disabled, with the grant tool', async () => {
    const h = harness({ disabled: ['browser_click'] })
    const list = h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(list)) as TurnToolsResult
    expect(tools.map((t) => t.name).sort()).toEqual(
      [`${BROWSER_SERVER}__browser_navigate`, `${BROWSER_SERVER}__browser_snapshot`, 'scadbuddy_browser__authorize_request'].sort(),
    )
    expect(tools.find((t) => t.name === NAV)?.task_queue).toBeUndefined() // no host queue in this harness
    expect(tools.find((t) => t.name === 'scadbuddy_browser__authorize_request')?.may_park).toBe(true)
    // A navigation is outward by the origin it opens, so it is listed as one that may park
    // (review of #2324); a snapshot is read whatever its input.
    expect(tools.find((t) => t.name === NAV)?.may_park).toBe(true)
    expect(tools.find((t) => t.name === `${BROWSER_SERVER}__browser_snapshot`)?.may_park).toBe(false)
  })

  it('offers nothing, and starts no browser, while Settings has it off', async () => {
    const h = harness({ enabled: false })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(tools).toEqual([])
    expect(h.started()).toBe(0)
  })

  it("tiers a navigation by its origin: ScadBuddy's own is read, an allowed one outward, any other refused", async () => {
    const h = harness()
    const tier = (url: string) => env(TURN_TOOL_TIER_ACTIVITY).run(h.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: NAV, input: { url } })
    expect(await tier(`${BACKEND}/models`)).toEqual({ tier: 'read' })
    expect(await tier('https://makerworld.com/x')).toEqual({ tier: 'outward' })
    // Refused calls are not parked: the call says why.
    expect(await tier('https://evil.example/')).toEqual({ tier: 'read' })
    await expect(env(`turn:${NAV}`).run(h.call, { url: 'https://evil.example/' })).rejects.toThrow(/not opened/)
    await expect(env(`turn:${BROWSER_SERVER}__browser_snapshot`).run(h.call, { filename: '../../etc/passwd' })).rejects.toThrow(/plain file name/)
    expect(h.calls).toEqual([])
  })

  it('tiers a call without starting a browser: the check may run on a replica the calls are not pinned to (#2523)', async () => {
    const h = harness({ disabled: ['browser_click'] })
    const tier = (tool: string) => env(TURN_TOOL_TIER_ACTIVITY).run(h.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool, input: {} })
    expect(await tier(`${BROWSER_SERVER}__browser_snapshot`)).toEqual({ tier: 'read' })
    await expect(tier(`${BROWSER_SERVER}__browser_click`)).rejects.toThrow(/not offered any more/)
    await expect(tier(`${BROWSER_SERVER}__browser_evaluate`)).rejects.toThrow(/not offered any more/)
    expect(h.started()).toBe(0)
  })

  it('gives back every use of the browser it takes, listing and call alike', async () => {
    const h = harness()
    await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)
    await env(`turn:${BROWSER_SERVER}__browser_snapshot`).run(h.call, {})
    expect(h.released()).toBe(h.started())
    expect(h.released()).toBeGreaterThan(0)
  })

  it("reads the UI probe once a turn: a probe whose answer flips between a turn's calls does not change the browser's options (#2522)", async () => {
    let answer: UiReach = { reach: 'ok' }
    let probes = 0
    const h = harness({
      probe: async () => {
        probes++
        return { [BACKEND]: answer }
      },
    })
    await env(`turn:${BROWSER_SERVER}__browser_snapshot`).run(h.call, {})
    answer = { reach: 'unreachable', detail: 'down' }
    await env(`turn:${BROWSER_SERVER}__browser_snapshot`).run(h.call, {})
    expect(probes).toBe(1)
    expect(new Set(h.held).size).toBe(1)
  })

  it("never reads the browser's options for another server's call", async () => {
    let probes = 0
    const h = harness({ probe: async () => (probes++, {}) })
    await expect(env('turn:scadbuddy_browser__authorize_request').run(h.call, {})).rejects.toThrow()
    expect(probes).toBe(0)
  })

  it('runs an approved navigation to an allowed origin and remembers the origin under that call', async () => {
    const h = harness()
    await expect(env(`turn:${NAV}`).run(h.call, { url: 'https://makerworld.com/x' })).rejects.toThrow(/needs an approval/)
    expect(await env(`turn:${NAV}`, 'tool-toolu_approved').run(h.call, { url: 'https://makerworld.com/x' })).toBe('browser_navigate ok')
    expect(h.remembered).toEqual([['https://makerworld.com', `durable:${SESSION}:run-1:toolu_approved`, SESSION]].map(() => expect.arrayContaining(['https://makerworld.com'])))
    expect(h.approvedOrigins).toEqual(['https://makerworld.com'])
  })

  it('refuses a tool on the deny list even when named directly', async () => {
    const h = harness()
    await expect(env(`turn:${BROWSER_SERVER}__browser_evaluate`, 'tool-toolu_approved').run(h.call, {})).rejects.toThrow(/not offered any more/)
    expect(h.calls).toEqual([])
  })

})

describe('BrowserHost (#2522)', () => {
  const OPTIONS: HeadlessBrowserOptions = { sessionId: SESSION, backendUrl: BACKEND, dir: '/tmp/turn-browser-host', tmpDir: '/tmp/turn-browser-host-tmp' }

  function host(options: { active?: (session: string) => Promise<boolean> } = {}) {
    const closed: string[] = []
    let n = 0
    const b = new BrowserHost({
      paths: { stateDir: '/tmp/turn-browser-host-state' },
      idleMs: 1000,
      ...options,
      start: async (_o, key) => {
        const id = `browser-${++n}`
        // A start that takes a while, so two calls can race it.
        await new Promise((r) => setTimeout(r, 5))
        const client = { close: async () => void closed.push(id) } as unknown as Hosted['client']
        return { client, plugin: {} as HeadlessBrowserPlugin, key, lastUsed: 0, inFlight: 0, id } as Hosted
      },
    })
    return { b, closed, starts: () => n }
  }
  const idOf = (h: Hosted) => (h as Hosted & { id: string }).id

  it('reuses the running browser for the same settings, whatever the probe answered', async () => {
    const { b, closed, starts } = host()
    const one = await b.browser(SESSION, { ...OPTIONS, uiReach: { [BACKEND]: { reach: 'ok' } } })
    b.release(one)
    const two = await b.browser(SESSION, { ...OPTIONS, uiReach: { [BACKEND]: { reach: 'unreachable', detail: 'down' } }, approvedOrigins: ['https://makerworld.com'] })
    b.release(two)
    expect(idOf(two)).toBe(idOf(one))
    expect(starts()).toBe(1)
    expect(closed).toEqual([])
    await b.stop()
  })

  it('starts one browser for calls that race, and restarts it for new settings only once the call in flight is done', async () => {
    const { b, closed, starts } = host()
    const [a, c] = await Promise.all([b.browser(SESSION, OPTIONS), b.browser(SESSION, OPTIONS)])
    expect(idOf(a)).toBe(idOf(c))
    expect(starts()).toBe(1)
    b.release(c)
    // `a` is still in flight: the restart waits for it.
    let restarted: Hosted | undefined
    const restart = b.browser(SESSION, { ...OPTIONS, sandbox: true }).then((h) => (restarted = h))
    await new Promise((r) => setTimeout(r, 20))
    expect(restarted).toBeUndefined()
    expect(closed).toEqual([])
    b.release(a)
    await restart
    expect(closed).toEqual([idOf(a)])
    expect(idOf(restarted!)).not.toBe(idOf(a))
    b.release(restarted!)
    await b.stop()
  })

  it('reaps an idle browser, but not one with a call in flight, nor one whose turn is still running', async () => {
    let running = true
    const { b, closed } = host({ active: async () => running })
    const h = await b.browser(SESSION, OPTIONS)
    await b.reap(Date.now() + 5000)
    expect(closed).toEqual([]) // in flight
    b.release(h)
    await b.reap(Date.now() + 5000)
    expect(closed).toEqual([]) // the turn runs (parked on an approval, say)
    running = false
    await b.reap(Date.now() + 500)
    expect(closed).toEqual([]) // not idle long enough
    await b.reap(Date.now() + 5000)
    expect(closed).toEqual([idOf(h)])
    await b.stop()
  })
})
