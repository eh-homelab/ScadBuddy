import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { HeadlessBrowserPlugin } from '../src/harness/headlessBrowser.js'
import type { BuiltInPolicy } from '../src/plugins/builtInTools.js'
import type { Owner } from '../src/sessions/protocol.js'
import { BROWSER_SERVER, type Hosted, turnBrowserServers } from '../src/temporal/turnBrowser.js'
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

function harness(options: { enabled?: boolean; disabled?: string[] } = {}) {
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
  const host = {
    browser: async (): Promise<Hosted> => {
      started++
      const plugin = { approve: (o: string) => void approvedOrigins.push(o) } as unknown as HeadlessBrowserPlugin
      return { client: await connectInProcess(fakeBrowser(calls)), plugin, key: '', lastUsed: 0 }
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
    headless: { backendUrl: BACKEND, browserAllowedOrigins: 'https://makerworld.com' },
    settings: { get: async <T,>(key: string) => (key === 'headless_browser_enabled' ? (options.enabled ?? true) : undefined) as T },
    builtIn: async () => policy,
  })
  const deps = {
    sessions: { ownerOf: async (id: string) => (id === SESSION ? BROWSER : undefined) },
    servers: async (context: Parameters<typeof servers>[0]) => ({ servers: await servers(context), problems: [], secrets: [], release: () => {} }),
    approvals: { approved: async (requestId: string) => requestId.endsWith(':toolu_approved') },
  }
  return { calls, remembered, approvedOrigins, started: () => started, activities: turnToolActivities(deps), call: turnToolActivity(deps) }
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
