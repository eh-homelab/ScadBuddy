import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import type { AuditEntry } from '../src/audit/log.js'
import type { RiskTier } from '../src/harness/permissions.js'
import type { Owner } from '../src/sessions/protocol.js'
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
    server.registerTool('broken', { description: 'fails', inputSchema: {} }, async () => ({
      isError: true,
      content: [{ type: 'text', text: 'it broke' }],
    }))
    return server
  }
}

// read.note is read; post is read for GET and outward otherwise, as http_request is tiered.
const tierOf = (name: string, input: unknown): RiskTier => {
  if (name.endsWith('__read_note')) return 'read'
  if (name.endsWith('__post')) return (input as { method?: string } | undefined)?.method === 'GET' ? 'read' : 'outward'
  return 'outward'
}

function harness(options: { owner?: Owner & { turnTiers?: string[] }; offered?: boolean; problems?: string[] } = {}) {
  const calls: string[] = []
  const audits: AuditEntry[] = []
  const reported: string[] = []
  const owner = options.owner ?? BROWSER
  const server: TurnServer = { name: 'notes', connect: () => connectInProcess(notes(calls)), tierOf }
  const deps: TurnToolsDeps = {
    sessions: { ownerOf: async (id) => (id === SESSION ? (owner as never) : undefined) },
    servers: async () => ({ servers: options.offered === false ? [] : [server], problems: options.problems ?? [], release: () => {} }),
    approvals: { approved: async (requestId) => requestId.endsWith(':toolu_approved') },
    audit: {
      record: async (entry) => void audits.push(entry),
      hash: (tool) => `hash:${tool}`,
      summarise: (tool) => `summary:${tool}`,
    },
    report: async (_session, problems) => void reported.push(...problems),
  }
  return { calls, audits, reported, activities: turnToolActivities(deps), call: turnToolActivity(deps) }
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
      ['notes__broken', true],
    ])
    expect(tools[0]!.input_schema).toMatchObject({ type: 'object', properties: { id: { type: 'string' } } })
    // A configured server left out is said in the session, as a classic turn says it.
    expect(h.reported).toEqual(['plugin hindsight failed its check'])
  })

  it('tiers a call from its input', async () => {
    const h = harness()
    const tier = (input: unknown) => env(TURN_TOOL_TIER_ACTIVITY).run(h.activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'notes__post', input })
    expect(await tier({ method: 'GET', body: '' })).toEqual({ tier: 'read' })
    expect(await tier({ method: 'POST', body: '' })).toEqual({ tier: 'outward' })
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

  it('refuses a workflow that is no durable session', async () => {
    const refused = await failure(env('turn:notes__read_note', 'tool-toolu_01', 'flow-x').run(harness().call, { id: 'a' }))
    expect(refused.type).toBe('UnknownSession')
  })
})
