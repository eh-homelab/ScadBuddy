import { mkdir, mkdtemp, readdir } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { PluginsForRun } from '../src/plugins/forwarder.js'
import type { AuditEntry } from '../src/audit/log.js'
import type { Tier } from '../src/auth/principal.js'
import type { RiskTier } from '../src/harness/permissions.js'
import type { Owner } from '../src/sessions/protocol.js'
import { HTTP_READ_TOOL, HTTP_SERVER, HTTP_TOOL } from '../src/harness/httpRequest.js'
import { FLOW_BODIES_KEPT_MS, removeTurnDir, sweepTurnDirs, turnServers } from '../src/temporal/turnServers.js'
import { FLOW_IMAGES_FULL } from '../src/temporal/toolActivities.js'
import {
  connectInProcess,
  TURN_TOOL_TIER_ACTIVITY,
  TURN_TOOLS_ACTIVITY,
  turnToolActivities,
  turnToolActivity,
  MAX_TURN_TOOL_NAME,
  type TurnContext,
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
const FLOW = '7c2d9a10-3b4e-4f5a-8b6c-1d2e3f4a5b6c'

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
  if (name.endsWith('__read_note') || name.endsWith('__shot')) return 'read'
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
    flowStarter?: Owner
    starterTiers?: Tier[]
    server?: (calls: string[]) => () => McpServer
    mayPark?: boolean
    fixedTier?: (name: string) => RiskTier | undefined
    listingKey?: string
    flowBlobs?: TurnToolsDeps['flowBlobs']
  } = {},
) {
  const contexts: TurnContext[] = []
  const calls: string[] = []
  const audits: AuditEntry[] = []
  const reported: string[] = []
  const stored: string[] = []
  const owner = options.owner ?? BROWSER
  let connects = 0
  const server: TurnServer = {
    name: 'notes',
    connect: () => (connects++, connectInProcess((options.server ?? notes)(calls))),
    ...(options.listingKey === undefined ? {} : { listingKey: options.listingKey }),
    tierOf,
    ...(options.mayPark === false ? {} : { mayPark }),
    ...(options.fixedTier ? { fixedTier: options.fixedTier } : {}),
    ...(options.pinned ? { pinned: true } : {}),
  }
  const deps: TurnToolsDeps = {
    sessions: { ownerOf: async (id) => (id === SESSION ? (owner as never) : undefined) },
    servers: async (context) => ({
      ...(void contexts.push(context), {}),
      servers: options.offered === false ? [] : [server],
      problems: options.problems ?? [],
      secrets: [SECRET],
      release: () => {},
    }),
    hostQueue: 'agent-tools.host-1',
    approvals: { approved: async (requestId) => requestId.endsWith(':toolu_approved') },
    flows: {
      startedBy: async (run) => (run === FLOW ? (options.flowStarter ?? BROWSER) : undefined),
      // Only the backend's workflow_run_decisions approves a flow's call, and only for the
      // input it was decided on (PgFlowRuns refuses a call with no hash, #2728).
      approved: async (requestId, inputHash) =>
        requestId === `flow:${FLOW}:run-1:flow_approved` && inputHash === `hash:notes__post:${JSON.stringify({ method: 'POST', body: 'x' })}`,
    },
    currentTiers: async () => options.starterTiers,
    audit: {
      record: async (entry) => void audits.push(entry),
      hash: (tool, input) => `hash:${tool}:${JSON.stringify(input)}`,
      summarise: (tool) => `summary:${tool}`,
    },
    report: async (_session, problems) => void reported.push(...problems),
    blobs: { put: async (session, images, toolUseId) => void stored.push(...images.map((i) => `${session}/${i.name}/${toolUseId}`)) },
    ...(options.flowBlobs ? { flowBlobs: options.flowBlobs } : {}),
  }
  return {
    contexts,
    calls,
    audits,
    reported,
    stored,
    connects: () => connects,
    activities: turnToolActivities(deps),
    call: turnToolActivity(deps),
  }
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

  it("lists a tool's tier where it does not depend on the input, for the transcript's label", async () => {
    const h = harness({ fixedTier: (name) => (name.endsWith('__read_note') ? 'read' : name.endsWith('__shot') ? 'write' : undefined) })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    const tiers = Object.fromEntries(tools.map((t) => [t.name, t.tier]))
    expect(tiers['notes__read_note']).toBe('read')
    expect(tiers['notes__shot']).toBe('write')
    // post's tier is its method's: no fixed tier, so none is listed.
    expect('tier' in tools.find((t) => t.name === 'notes__post')!).toBe(false)
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

  it('leaves out a tool whose turn name is over 50 characters and says so, listing the rest', async () => {
    const long = 'browser_console_messages_and_then_some_more_words'
    const withLong = (calls: string[]) => () => {
      const server = notes(calls)()
      server.registerTool(long, { description: 'too long', inputSchema: {} }, async () => ({ content: [] }))
      return server
    }
    const h = harness({ server: withLong })
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(h.activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    expect(`notes__${long}`.length).toBeGreaterThan(MAX_TURN_TOOL_NAME)
    expect(tools.map((t) => t.name)).not.toContain(`notes__${long}`)
    expect(tools.map((t) => t.name)).toContain('notes__post')
    expect(h.reported).toEqual([`tool ${long} of plugin notes is not available in this turn: its name notes__${long} is over 50 characters`])
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

  it("labels and parks a remote plugin's tools by their Settings tier, an unlisted one outward (#2838)", async () => {
    const forwarded: PluginsForRun = {
      plugins: [
        {
          name: 'notes',
          url: 'http://127.0.0.1:9/forward/notes',
          toolTiers: { search: 'read', save: 'write' },
          disabledTools: [],
        },
      ],
      problems: [],
      secrets: [],
      terminate: async () => {},
      release: () => {},
    }
    const { servers } = await turnServers({ remotePlugins: async () => forwarded })({
      session: SESSION,
      owner: BROWSER,
      signal: new AbortController().signal,
      listing: true,
    })
    const notes = servers.find((s) => s.name === 'notes')!
    const tier = (tool: string) => notes.fixedTier?.(`mcp__notes__${tool}`)
    expect([tier('search'), tier('save'), tier('delete')]).toEqual(['read', 'write', 'outward'])
    expect(['search', 'save', 'delete'].map((t) => notes.mayPark?.(`mcp__notes__${t}`))).toEqual([false, false, true])
    expect(notes.tierOf('mcp__notes__delete', {})).toBe('outward')
  })

  it("lists the real http_request as may_park, and tiers a POST outward and refuses it unapproved (review of #2324)", async () => {
    const saveRoot = await mkdtemp(path.join(os.tmpdir(), 'turn-http-'))
    const deps: TurnToolsDeps = {
      sessions: { ownerOf: async (id) => (id === SESSION ? (BROWSER as never) : undefined) },
      // Its saved bodies are in Postgres; this test saves none.
      servers: turnServers({ httpRequest: { saveRoot, sql: {} as never } }),
      approvals: { approved: async () => false },
    }
    const activities = turnToolActivities(deps)
    const { tools } = (await env(TURN_TOOLS_ACTIVITY).run(activities[TURN_TOOLS_ACTIVITY] as () => Promise<TurnToolsResult>)) as TurnToolsResult
    const parks = Object.fromEntries(tools.map((t) => [t.name, t.may_park]))
    expect(parks[`${HTTP_SERVER}__${HTTP_TOOL}`]).toBe(true)
    expect(parks[`${HTTP_SERVER}__${HTTP_READ_TOOL}`]).toBe(false)
    const labels = Object.fromEntries(tools.map((t) => [t.name, t.tier]))
    expect(labels[`${HTTP_SERVER}__${HTTP_TOOL}`]).toBeUndefined()
    expect(labels[`${HTTP_SERVER}__${HTTP_READ_TOOL}`]).toBe('read')
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

  it("records the durable turn on a call's audit row and in its servers' context, as a classic turn's (#2497)", async () => {
    const turnId = '6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
    const h = harness({ owner: { ...BROWSER, turnId } as never })
    await env('turn:notes__read_note').run(h.call, { id: 'a' })
    expect(h.audits).toEqual([expect.objectContaining({ turnId })])
    expect(h.contexts.at(-1)?.turnId).toBe(turnId)
  })

  it('lists a keyed server once for a tier check and the call after it (#2497)', async () => {
    const h = harness({ listingKey: 'k1' })
    await env(TURN_TOOL_TIER_ACTIVITY).run(h.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'notes__read_note', input: {} })
    await env('turn:notes__read_note').run(h.call, { id: 'a' })
    // One listing, kept; then the call's own connection.
    expect(h.connects()).toBe(2)
    // An unkeyed server is listed for each.
    const u = harness()
    await env(TURN_TOOL_TIER_ACTIVITY).run(u.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'notes__read_note', input: {} })
    await env('turn:notes__read_note').run(u.call, { id: 'a' })
    expect(u.connects()).toBe(3)
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
    const [session, name, toolUseId] = h.stored[0]!.split('/')
    expect(session).toBe(SESSION)
    // The call that produced it, which the panel's tool.result names it under (#2478).
    expect(toolUseId).toBe('toolu_approved')
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

  describe("from a flow's tool(name, args) (#2320 phase 8)", () => {
    const flowEnv = (activityType: string, activityId = 'tool-flow_01') => env(activityType, activityId, `flow-${FLOW}`)

    it("keeps a call's images under the run, as its own tools' are, and answers pointers (#2578)", async () => {
      const kept: string[] = []
      const flowBlobs = {
        put: async (subject: string, images: readonly { name: string }[]) => {
          kept.push(...images.map((i) => `${subject}/${i.name}`))
          return images.map((i) => i.name)
        },
        get: async () => undefined,
      }
      const h = harness({ flowBlobs })
      const raw: unknown = await flowEnv('turn:notes__shot').run(h.call as (input: unknown) => Promise<unknown>, {})
      const out = raw as {
        text: string
        images: { name: string; mediaType: string }[]
      }
      expect(h.stored).toEqual([])
      expect(kept).toHaveLength(1)
      const [name] = out.images.map((i) => i.name)
      expect(kept[0]).toBe(`flow-${FLOW}/${name}`)
      expect(out.images[0]!.mediaType).toBe('image/png')
      expect(out.text).toContain(`[image ${name}]`)
      expect(out.text).not.toContain('view_tool_images')
      expect(JSON.stringify(out)).not.toContain('iVBORw0KGgo')
    })

    it("says an image the run did not keep, with no pointer, when its run keeps no more (#2578)", async () => {
      const flowBlobs = { put: async () => [], get: async () => undefined }
      const h = harness({ flowBlobs })
      const out = await flowEnv('turn:notes__shot').run(h.call as (input: unknown) => Promise<unknown>, {})
      expect(out).toBe(FLOW_IMAGES_FULL)
    })

    it('keeps no image from an errored call: nothing would point at it', async () => {
      const kept: string[] = []
      const flowBlobs = {
        put: async (subject: string, images: readonly { name: string }[]) => {
          kept.push(...images.map((i) => `${subject}/${i.name}`))
          return images.map((i) => i.name)
        },
        get: async () => undefined,
      }
      const failing = () => () => {
        const server = new McpServer({ name: 'notes', version: '1' })
        server.registerTool('shot', { description: 'a picture that fails', inputSchema: {} }, async () => ({
          isError: true,
          content: [
            { type: 'text', text: 'it broke' },
            { type: 'image', data: 'iVBORw0KGgo=', mimeType: 'image/png' },
          ],
        }))
        return server
      }
      const h = harness({ flowBlobs, server: failing })
      const err = await failure(flowEnv('turn:notes__shot').run(h.call as (input: unknown) => Promise<unknown>, {}))
      expect(err.message).toContain('it broke')
      expect(kept).toEqual([])
    })

    it("tiers and runs a read call as the run's starter, with no session and no browser", async () => {
      const h = harness({ pinned: true })
      const tier = h.activities[TURN_TOOL_TIER_ACTIVITY]!
      expect(await flowEnv(TURN_TOOL_TIER_ACTIVITY).run(tier, { tool: 'notes__read_note', input: { id: 'a' } })).toEqual({
        tier: 'read',
        task_queue: 'agent-tools.host-1',
      })
      expect(await flowEnv('turn:notes__read_note').run(h.call, { id: 'a' })).toBe('note a')
      expect(h.contexts.every((c) => c.flow === FLOW && c.session === `flow-${FLOW}`)).toBe(true)
      expect(h.audits).toEqual([
        expect.objectContaining({ action: 'notes__read_note', sessionId: null, requestId: `flow:${FLOW}:run-1:flow_01` }),
      ])
    })

    it("runs an outward call only with the backend's decision for it, never a session approval", async () => {
      const h = harness()
      const refused = await failure(flowEnv('turn:notes__post', 'tool-toolu_approved').run(h.call, { method: 'POST', body: 'x' }))
      expect(refused.type).toBe('NotApproved')
      expect(h.calls).toEqual([])
      expect(await flowEnv('turn:notes__post', 'tool-flow_approved').run(h.call, { method: 'POST', body: 'x' })).toBe('posted x')
      // #2728: the decision binds the input it approved; another input is refused.
      const changed = await failure(flowEnv('turn:notes__post', 'tool-flow_approved').run(h.call, { method: 'POST', body: 'y' }))
      expect(changed.type).toBe('NotApproved')
      expect(h.calls).toEqual(['post x'])
    })

    it('holds a token starter to the tiers its token holds now', async () => {
      const h = harness({ flowStarter: BEARER, starterTiers: ['read'] })
      const refused = await failure(flowEnv('turn:notes__post', 'tool-flow_approved').run(h.call, { method: 'POST', body: 'x' }))
      expect(refused.message).toContain("which this flow run's starter does not hold")
      expect(h.calls).toEqual([])
    })

    it('refuses a run that is not recorded, and a tool no server offers, before any call', async () => {
      const h = harness()
      const unrecorded = env(TURN_TOOL_TIER_ACTIVITY, 'x', 'flow-00000000-0000-4000-8000-000000000000')
      const unknown = await failure(unrecorded.run(h.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'notes__post', input: {} }))
      expect(unknown.type).toBe('UnknownFlow')
      const none = harness({ offered: false })
      const gone = await failure(flowEnv(TURN_TOOL_TIER_ACTIVITY).run(none.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'notes__post', input: {} }))
      expect(gone.message).toContain('not offered')
      expect(h.calls).toEqual([])
    })
  })
})

describe('saved http bodies on this pod (#2496)', () => {
  const GONE = '1b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61'
  const sql = ((_s: TemplateStringsArray, ...values: unknown[]) =>
    Promise.resolve(((values[0] as string[]) ?? []).filter((id) => id === SESSION).map((id) => ({ id })))) as never

  it("sweeps a session's bodies once its row is gone, and a flow run's once old; keeps the rest", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'turn-dirs-'))
    for (const name of [SESSION, GONE, `flow-${FLOW}`, 'not-ours']) await mkdir(path.join(root, name, 'http'), { recursive: true })
    expect((await sweepTurnDirs(root, sql)).sort()).toEqual([GONE])
    expect(await sweepTurnDirs(root, sql, Date.now() + FLOW_BODIES_KEPT_MS + 1000)).toEqual([`flow-${FLOW}`])
    expect((await readdir(root)).sort()).toEqual([SESSION, 'not-ours'].sort())
    expect(await sweepTurnDirs(path.join(root, 'missing'), sql)).toEqual([])
  })

  it("removes a forgotten subject's bodies, and only a subject's", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'turn-dirs-'))
    await mkdir(path.join(root, SESSION, 'http'), { recursive: true })
    await mkdir(path.join(root, `flow-${FLOW}`), { recursive: true })
    await removeTurnDir(root, `session-${SESSION}`)
    await removeTurnDir(root, `flow-${FLOW}`)
    await removeTurnDir(root, '..')
    expect(await readdir(root)).toEqual([])
  })
})
