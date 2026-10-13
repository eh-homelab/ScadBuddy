import { randomBytes, randomUUID } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { inputHash } from '../src/approvals/service.js'
import { flowRequestId } from '../src/gate/ids.js'
import { PgFlowRuns } from '../src/temporal/flowRuns.js'
import { connectInProcess, turnToolActivity } from '../src/temporal/turnTools.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// A flow run's starter and decisions, read from the backend's own tables (#1057, plan
// 2026-10-09 Task D1). The tables come from the backend's migration files, copied into
// fixtures/backend-flow-migrations: the agent never migrates them.

const MIGRATIONS = fileURLToPath(new URL('./fixtures/backend-flow-migrations/', import.meta.url))

describe.skipIf(!TEST_DATABASE_URL)(`PgFlowRuns${TEST_DATABASE_URL ? '' : ` (set ${TEST_DATABASE_URL_ENV})`}`, () => {
  let t: Throwaway
  let flows: PgFlowRuns
  beforeEach(async () => {
    t = await throwawayDatabase()
    for (const file of readdirSync(MIGRATIONS).sort()) await t.db.sql.unsafe(readFileSync(`${MIGRATIONS}${file}`, 'utf8'))
    await t.db.sql`
      INSERT INTO workflow_definitions (id, name, version, script, created_by)
      VALUES ('d1', 'swap', 1, 'x', '{"kind": "browser"}')`
    flows = new PgFlowRuns(t.db.sql)
  })
  afterEach(async () => {
    await t?.drop()
  })

  async function run(startedBy: object): Promise<string> {
    const id = randomUUID()
    await t.db.sql`
      INSERT INTO workflow_runs (id, definition_id, version, name, status, workflow_id, workflow_run_id, started_by)
      VALUES (${id}, 'd1', 1, 'swap', 'running', ${`flow-${id}`}, 'wr1', ${t.db.sql.json(startedBy as never)})`
    return id
  }

  it("names a run's starter: the browser, a session's owner, or the author's principal", async () => {
    const session = randomUUID()
    await t.db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${session}, 'mcp', 'bearer', 'token:a', 'Agent A', 'bearer', 'token:a', 'idle', 10, 1)`
    expect(await flows.startedBy(await run({ kind: 'browser' }), 'wr1')).toEqual({ kind: 'browser', id: 'browser', label: 'You' })
    expect(await flows.startedBy(await run({ kind: 'agent', principal: 'token:a', session }), 'wr1')).toEqual({
      kind: 'bearer',
      id: 'token:a',
      label: 'Agent A',
    })
    expect(await flows.startedBy(await run({ kind: 'agent', principal: 'oidc:https%3A%2F%2Fidp#u1', session: null }), 'wr1')).toEqual({
      kind: 'oidc',
      id: 'oidc:https://idp#u1',
      label: 'MCP OIDC oidc:https://idp#u1',
    })
    expect(await flows.startedBy(await run({ kind: 'agent', principal: 'someone' }), 'wr1')).toBeUndefined()
    expect(await flows.startedBy(randomUUID(), 'wr1')).toBeUndefined()
  })

  // #2258: a closed run lends its starter to no later flow-<id> execution, nor does an open
  // one to an execution a Reset replaced.
  it('names no starter once the run has closed, or for a superseded execution', async () => {
    const id = await run({ kind: 'browser' })
    expect(await flows.startedBy(id, 'wr1')).toEqual({ kind: 'browser', id: 'browser', label: 'You' })
    await t.db.sql`UPDATE workflow_runs SET status = 'waiting' WHERE id = ${id}`
    expect(await flows.startedBy(id, 'wr1')).toEqual({ kind: 'browser', id: 'browser', label: 'You' })
    await t.db.sql`UPDATE workflow_runs SET superseded_run_ids = '{wr0}' WHERE id = ${id}`
    expect(await flows.startedBy(id, 'wr0')).toBeUndefined()
    expect(await flows.startedBy(id, 'wr1')).toEqual({ kind: 'browser', id: 'browser', label: 'You' })
    for (const status of ['succeeded', 'failed', 'terminated']) {
      await t.db.sql`UPDATE workflow_runs SET status = ${status} WHERE id = ${id}`
      expect(await flows.startedBy(id, 'wr2'), status).toBeUndefined()
    }
  })

  it("approves only a call whose decision is an approval that approved, for the same input", async () => {
    const id = await run({ kind: 'browser' })
    const decide = async (call: string, kind: string, outcome: string, hash: string | null = 'h1') =>
      t.db.sql`
        INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id, call_id, kind, outcome, responder, input_hash)
        VALUES (${flowRequestId(id, 'wr1', call)}, ${id}, 'wr1', ${call}, ${kind}, ${outcome}, 'browser', ${hash})`
    await decide('c1', 'approval', 'approved')
    await decide('c2', 'approval', 'denied')
    await decide('c3', 'answer', 'answered')
    await decide('c5', 'approval', 'approved', null)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c1'), 'h1')).toBe(true)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c2'), 'h1')).toBe(false)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c3'), 'h1')).toBe(false)
    // Another execution's (a Reset's) or a call never decided.
    expect(await flows.approved(flowRequestId(id, 'wr2', 'c1'), 'h1')).toBe(false)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c4'), 'h1')).toBe(false)
    // #2318: another tool or input, no hash to compare, or a decision that recorded none.
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c1'), 'h2')).toBe(false)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c1'))).toBe(false)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c5'), 'h1')).toBe(false)
  })

  // #2481 (plan 2026-10-11 Task A4): a call the run's grant approved runs only while the
  // grant is in force; one revoked or expired since is refused GrantRevoked, not NotApproved.
  it("approves a grant's call only while the grant is in force, and names a lapsed one", async () => {
    const id = await run({ kind: 'browser' })
    await t.db.sql`
      INSERT INTO flow_grants (id, run_id, status, spec, requested_by, approved_by, approved_at, expires_at)
      VALUES ('g1', ${id}, 'approved', '{}', '{"kind": "browser"}', 'browser', now(), now() + interval '1 hour')`
    const decide = async (call: string, responder: string) =>
      t.db.sql`
        INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id, call_id, kind, outcome, responder, input_hash)
        VALUES (${flowRequestId(id, 'wr1', call)}, ${id}, 'wr1', ${call}, 'approval', 'approved', ${responder}, 'h1')`
    await decide('c1', 'grant:g1')
    await decide('c2', 'browser')
    const c1 = flowRequestId(id, 'wr1', 'c1')
    const c2 = flowRequestId(id, 'wr1', 'c2')
    expect(await flows.approved(c1, 'h1')).toBe(true)
    expect(await flows.lapsed(c1, 'h1')).toBe(false)
    // Changed input is #2318's refusal, whatever the grant.
    expect(await flows.approved(c1, 'h2')).toBe(false)
    expect(await flows.lapsed(c1, 'h2')).toBe(false)
    await t.db.sql`UPDATE flow_grants SET revoked_at = now(), revoked_by = 'browser' WHERE id = 'g1'`
    expect(await flows.approved(c1, 'h1')).toBe(false)
    expect(await flows.lapsed(c1, 'h1')).toBe(true)
    // A person's approval does not depend on the grant.
    expect(await flows.approved(c2, 'h1')).toBe(true)
    expect(await flows.lapsed(c2, 'h1')).toBe(false)
    await t.db.sql`UPDATE flow_grants SET revoked_at = NULL, expires_at = now() - interval '1 second' WHERE id = 'g1'`
    expect(await flows.approved(c1, 'h1')).toBe(false)
    expect(await flows.lapsed(c1, 'h1')).toBe(true)
  })

  // #2728: a flow's outward turn tool runs with its recorded approval, bound to the input.
  it('runs an approved outward turn tool in a flow, and refuses another input', async () => {
    const id = await run({ kind: 'browser' })
    const calls: string[] = []
    const server = () => {
      const s = new McpServer({ name: 'notes', version: '1' })
      s.registerTool('post', { description: 'posts', inputSchema: { body: z.string() } }, async ({ body }) => {
        calls.push(body)
        return { content: [{ type: 'text', text: `posted ${body}` }] }
      })
      return s
    }
    // The audit's own hashing, as the worker wires it (#2871: not a stand-in).
    const key = randomBytes(32)
    const hash = (tool: string, input: Record<string, unknown>) => inputHash(key, tool, input)
    const call = turnToolActivity({
      sessions: { ownerOf: async () => undefined },
      servers: async () => ({
        servers: [{ name: 'notes', connect: () => connectInProcess(server), tierOf: () => 'outward' }],
        problems: [],
        secrets: [],
        release: () => {},
      }),
      hostQueue: 'agent-tools.host-1',
      flows,
      audit: { record: async () => {}, hash, summarise: (tool) => tool },
    })
    await t.db.sql`
      INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id, call_id, kind, outcome, responder, input_hash)
      VALUES (${flowRequestId(id, 'wr1', 'c1')}, ${id}, 'wr1', 'c1', 'approval', 'approved', 'browser',
        ${hash('notes__post', { body: 'x' })})`
    const env = () =>
      new MockActivityEnvironment({
        activityId: 'tool-c1',
        activityType: 'turn:notes__post',
        workflowExecution: { workflowId: `flow-${id}`, runId: 'wr1' },
        taskQueue: 'agent-tools',
      })
    expect(await env().run(call, { body: 'x' })).toBe('posted x')
    const refused = await env()
      .run(call, { body: 'y' })
      .then(
        () => undefined,
        (e: unknown) => e,
      )
    expect(refused).toBeInstanceOf(ApplicationFailure)
    expect((refused as ApplicationFailure).type).toBe('NotApproved')
    expect(calls).toEqual(['x'])

    // #2871: a call the run's grant approved, revoked since, is refused as GrantRevoked.
    await t.db.sql`
      INSERT INTO flow_grants (id, run_id, status, spec, requested_by, approved_by, approved_at, expires_at, revoked_at, revoked_by)
      VALUES ('g1', ${id}, 'approved', '{}', '{"kind": "browser"}', 'browser', now(), now() + interval '1 hour', now(), 'browser')`
    await t.db.sql`
      INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id, call_id, kind, outcome, responder, input_hash)
      VALUES (${flowRequestId(id, 'wr1', 'c2')}, ${id}, 'wr1', 'c2', 'approval', 'approved', 'grant:g1',
        ${hash('notes__post', { body: 'z' })})`
    const lapsed = await new MockActivityEnvironment({
      activityId: 'tool-c2',
      activityType: 'turn:notes__post',
      workflowExecution: { workflowId: `flow-${id}`, runId: 'wr1' },
      taskQueue: 'agent-tools',
    })
      .run(call, { body: 'z' })
      .then(
        () => undefined,
        (e: unknown) => e,
      )
    expect((lapsed as ApplicationFailure).type).toBe('GrantRevoked')
    expect(calls).toEqual(['x'])
  })
})
