import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { AuditEntry } from '../src/audit/log.js'
import type { RiskTier } from '../src/harness/permissions.js'
import type { Owner } from '../src/sessions/protocol.js'
import { HTTP_READ_TOOL, HTTP_SERVER, HTTP_TOOL } from '../src/harness/httpRequest.js'
import { turnServers } from '../src/temporal/turnServers.js'
import {
  connectInProcess,
  TURN_TOOL_TIER_ACTIVITY,
  TURN_TOOLS_ACTIVITY,
  turnToolActivities,
  turnToolActivity,
  type TurnServer,
  type TurnToolsDeps,
  type TurnToolsResult,
} from '../src/temporal/turnTools.js'

// A durable turn's plugin and http tools (#2320): listed per turn, tiered per call from
// the input, gated by the recorded approval, and refused once their server is gone.

const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const BROWSER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
const BEARER: Owner = { kind: 'bearer', id: 'token:7', label: 'MCP token' }
const SECRET = 'sk-plugin-header-0123456789'

function notes(calls: string[]): () => McpServer {
  return () => {
    const server = new McpServer({ name: 'notes', version: '1' })
    server.registerTool('read.note', { description: 'reads a note', inputSchema: { id: z.string() } }, async ({ id }) => {
      calls.push(`read ${id}`)
      return { content: [{ type: 'text', text: `note ${id}` }] }
    })
    server.registerTool('post', { description: 'posts', inputSchema: { method: z.string(), body: z.string() } }, async ({ body }) => {
      calls.push(`post ${body}`)
      return { content: [{ type: 'text', text: `posted ${body}` }] }
    })
    server.registerTool('shot', { description: 'a picture', inputSchema: {} }, async () => ({
      content: [{ type: 'image', data: 'iVBORw0KGgo=', mimeType: 'image/png' }],
    }))
    server.registerTool('broken', { description: 'fails', inputSchema: {} }, async () => ({
      isError: true,
      content: [{ type: 'text', text: 'it broke' }],
    }))
    // A plugin that echoes the header it was sent, as a classic turn's scrub expects one may.
    server.registerTool('echo', { description: 'echoes', inputSchema: {} }, async () => ({
      content: [{ type: 'text', text: `your token is ${SECRET}` }],
    }))
    server.registerTool('leak', { description: 'fails loudly', inputSchema: {} }, async () => ({
      isError: true,
      content: [{ type: 'text', text: `refused ${SECRET}` }],
    }))
    return server
  }
}

// read.note is read; post is tiered as httpTierOf tiers http_request: no method or a
// GET is read, anything else outward. So only its input says whether a call is outward.
const tierOf = (name: string, input: unknown): RiskTier => {
  if (name.endsWith('__read_note')) return 'read'
  if (name.endsWith('__post')) return ((input as { method?: string } | undefined)?.method ?? 'GET') === 'GET' ? 'read' : 'outward'
  return 'outward'
}
const mayPark = (name: string) => !name.endsWith('__read_note')

function harness(
  options: {
    owner?: Owner & { turnTiers?: string[] }
    offered?: boolean
    problems?: string[]
    pinned?: boolean
    server?: (calls: string[]) => () => McpServer
    mayPark?: boolean
  } = {},
) {
  const calls: string[] = []
  const audits: AuditEntry[] = []
  const reported: string[] = []
  const stored: string[] = []
  const owner = options.owner ?? BROWSER
  const server: TurnServer = {
    name: 'notes',
    connect: () => connectInProcess((options.server ?? notes)(calls)),
    tierOf,
    ...(options.mayPark === false ? {} : { mayPark }),
    ...(options.pinned ? { pinned: true } : {}),
  }
  const deps: TurnToolsDeps = {
    sessions: { ownerOf: async (id) => (id === SESSION ? (owner as never) : undefined) },
    servers: async () => ({
      servers: options.offered === false ? [] : [server],
      problems: options.problems ?? [],
      secrets: [SECRET],
      release: () => {},
    }),
    hostQueue: 'agent-tools.host-1',
    approvals: { approved: async (requestId) => requestId.endsWith(':toolu_approved') },
    audit: {
      record: async (entry) => void audits.push(entry),
      hash: (tool) => `hash:${tool}`,
      summarise: (tool) => `summary:${tool}`,
    },
    report: async (_session, problems) => void reported.push(...problems),
    blobs: { put: async (session, images) => void stored.push(...images.map((i) => `${session}/${i.name}`)) },
  }
  return { calls, audits, reported, stored, activities: turnToolActivities(deps), call: turnToolActivity(deps) }
}

function env(activityType: string, activityId = 'tool-toolu_01', workflowId = `session-${SESSION}`) {
  return new MockActivityEnvironment({ activityId, activityType, workflowExecution: { workflowId, runId: 'run-1' }, taskQueue: 'agent-tools' })
}

async function failure(promise: Promise<unknown>): Promise<ApplicationFailure> {
  const err = await promise.then(
    () => undefined,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(ApplicationFailure)
  return err as ApplicationFailure
}

describe('turn tools in a durable session', () => {
  it('lists each server tool by the name Claude Code gives it, and says which may park', async () => {
    const h = harness({ problems: ['plugin hindsight failed its check'] })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(tools.map((t) => [t.name, t.may_park])).toEqual([
      ['notes__read_note', false],
      ['notes__post', true],
      ['notes__shot', true],
      ['notes__broken', true],
      ['notes__echo', true],
      ['notes__leak', true],
    ])
    // Not pinned: its calls run on agent-tools, wherever.
    expect(tools.every((t) => t.task_queue === undefined)).toBe(true)
    expect(tools[0]!.input_schema).toMatchObject({ type: 'object', properties: { id: { type: 'string' } } })
    // A configured server left out is said in the session, as a classic turn says it.
    expect(h.reported).toEqual(['plugin hindsight failed its check'])
  })

  it('lists every tool of a server that cannot say which never park as may_park (fail toward asking)', async () => {
    const h = harness({ mayPark: false })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(tools.every((t) => t.may_park)).toBe(true)
  })

  it('leaves out tools whose names collide once Claude Code renames them, as a classic turn hides them', async () => {
    const colliding = (calls: string[]) => () => {
      const server = notes(calls)()
      server.registerTool('read_note', { description: 'another', inputSchema: {} }, async () => ({ content: [] }))
      return server
    }
    const h = harness({ server: colliding })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(tools.map((t) => t.name)).not.toContain('notes__read_note')
    expect((await failure(env('turn:notes__read_note').run(h.call, { id: 'a' }))).message).toContain('not offered any more')
  })

  it('leaves out a server whose listing stalls, says so once, and lists the others (review of #2324)', async () => {
    const reported: string[] = []
    const stalled: TurnServer = { name: 'stuck', connect: () => new Promise(() => {}), tierOf }
    const fine: TurnServer = { name: 'notes', connect: () => connectInProcess(notes([])), tierOf, mayPark }
    const deps: TurnToolsDeps = {
      sessions: { ownerOf: async (id) => (id === SESSION ? (BROWSER as never) : undefined) },
      servers: async () => ({ servers: [stalled, fine], problems: [], secrets: [], release: () => {} }),
      report: async (_session, problems) => void reported.push(...problems),
      listTimeoutMs: 50,
    }
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(turnToolActivities(deps)[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(tools.map((t) => t.name)).toContain('notes__post')
    expect(tools.some((t) => t.name.startsWith('stuck__'))).toBe(false)
    expect(reported).toEqual([expect.stringMatching(/^plugin stuck is not available in this turn/)])
  })

  it('tiers a call from its input', async () => {
    const h = harness()
    const tier = (input: unknown) => env(TURN_TOOL_TIER_ACTIVITY).run(h.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'notes__post', input })
    expect(await tier({ method: 'GET', body: '' })).toEqual({ tier: 'read' })
    expect(await tier({ method: 'POST', body: '' })).toEqual({ tier: 'outward' })
  })

  it("lists the real http_request as may_park, and tiers a POST outward and refuses it unapproved (review of #2324)", async () => {
    const saveRoot = await mkdtemp(path.join(os.tmpdir(), 'turn-http-'))
    const deps: TurnToolsDeps = {
      sessions: { ownerOf: async (id) => (id === SESSION ? (BROWSER as never) : undefined) },
      servers: turnServers({ httpRequest: { saveRoot } }),
      approvals: { approved: async () => false },
    }
    const activities = turnToolActivities(deps)
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    const parks = Object.fromEntries(tools.map((t) => [t.name, t.may_park]))
    expect(parks[`${HTTP_SERVER}__${HTTP_TOOL}`]).toBe(true)
    expect(parks[`${HTTP_SERVER}__${HTTP_READ_TOOL}`]).toBe(false)
    const tier = (input: unknown) =>
      env(TURN_TOOL_TIER_ACTIVITY).run(activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: `${HTTP_SERVER}__${HTTP_TOOL}`, input })
    expect(await tier({ url: 'http://example.test/', method: 'POST' })).toEqual({ tier: 'outward' })
    expect(await tier({ url: 'http://example.test/' })).toEqual({ tier: 'read' })
    const refused = await failure(
      env(`turn:${HTTP_SERVER}__${HTTP_TOOL}`).run(turnToolActivity(deps), { url: 'http://example.test/', method: 'POST' }),
    )
    expect(refused.type).toBe('NotApproved')
  })

  it('runs a read call and returns its text, with an audit row', async () => {
    const h = harness()
    expect(await env('turn:notes__read_note').run(h.call, { id: 'a' })).toBe('note a')
    expect(h.calls).toEqual(['read a'])
    expect(h.audits).toEqual([expect.objectContaining({ action: 'notes__read_note', tier: 'read', outcome: 'ok', sessionId: SESSION })])
  })

  it('runs an outward call only with an approval recorded for it', async () => {
    const h = harness()
    const refused = await failure(env('turn:notes__post').run(h.call, { method: 'POST', body: 'x' }))
    expect(refused.type).toBe('NotApproved')
    expect(h.calls).toEqual([])
    expect(await env('turn:notes__post', 'tool-toolu_approved').run(h.call, { method: 'POST', body: 'x' })).toBe('posted x')
    // A GET needs none.
    expect(await env('turn:notes__post').run(h.call, { method: 'GET', body: 'y' })).toBe('posted y')
  })

  it("holds a call to the turn's tiers", async () => {
    const h = harness({ owner: BEARER })
    const refused = await failure(env('turn:notes__post', 'tool-toolu_approved').run(h.call, { method: 'POST', body: 'x' }))
    expect(refused.message).toContain('needs the "outward" tier')
    expect(h.calls).toEqual([])
  })

  it('refuses a call whose server is gone, a tool error, and an activity that is no turn tool', async () => {
    expect((await failure(env('turn:notes__read_note').run(harness({ offered: false }).call, { id: 'a' }))).message).toContain('not offered any more')
    const broken = await failure(env('turn:notes__broken', 'tool-toolu_approved').run(harness().call, {}))
    expect(broken.message).toBe('it broke')
    expect(broken.nonRetryable).toBe(true)
    expect((await failure(env('something_else').run(harness().call, {}))).message).toContain('no activity something_else')
  })

  it("keeps a turn tool's images for view_tool_images (#2320)", async () => {
    const h = harness()
    const out = String(await env('turn:notes__shot', 'tool-toolu_approved').run(h.call as (input: unknown) => Promise<unknown>, {}))
    expect(h.stored).toHaveLength(1)
    const [session, name] = h.stored[0]!.split('/')
    expect(session).toBe(SESSION)
    expect(out).toContain(`[image ${name}]`)
    expect(out).toContain('view_tool_images')
  })

  it('refuses a workflow that is no durable session', async () => {
    const refused = await failure(env('turn:notes__read_note', 'tool-toolu_01', 'flow-x').run(harness().call, { id: 'a' }))
    expect(refused.type).toBe('UnknownSession')
  })

  it("pins a server's calls to the replica that listed it, when its state lives there", async () => {
    const h = harness({ pinned: true })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(new Set(tools.map((t) => t.task_queue))).toEqual(new Set(['agent-tools.host-1']))
  })

  it("redacts the turn's secrets from a result, an error and its audit row (security review of #2324)", async () => {
    const h = harness()
    const text = await env('turn:notes__echo', 'tool-toolu_approved').run(h.call, {})
    expect(text).toBe('your token is [redacted]')
    const refused = await failure(env('turn:notes__leak', 'tool-toolu_approved').run(h.call, {}))
    expect(refused.message).toBe('refused [redacted]')
    expect(JSON.stringify(h.audits)).not.toContain(SECRET)
    const reported = harness({ problems: [`plugin x failed with header ${SECRET}`] })
    await env(TURN_TOOLS_ACTIVITY).run(reported.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)
    expect(reported.reported).toEqual(['plugin x failed with header [redacted]'])
  })
})
