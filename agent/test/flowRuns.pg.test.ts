import { randomUUID } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flowRequestId } from '../src/gate/ids.js'
import { PgFlowRuns } from '../src/temporal/flowRuns.js'
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
    expect(await flows.startedBy(await run({ kind: 'browser' }))).toEqual({ kind: 'browser', id: 'browser', label: 'You' })
    expect(await flows.startedBy(await run({ kind: 'agent', principal: 'token:a', session }))).toEqual({
      kind: 'bearer',
      id: 'token:a',
      label: 'Agent A',
    })
    expect(await flows.startedBy(await run({ kind: 'agent', principal: 'oidc:https%3A%2F%2Fidp#u1', session: null }))).toEqual({
      kind: 'oidc',
      id: 'oidc:https://idp#u1',
      label: 'MCP OIDC oidc:https://idp#u1',
    })
    expect(await flows.startedBy(await run({ kind: 'agent', principal: 'someone' }))).toBeUndefined()
    expect(await flows.startedBy(randomUUID())).toBeUndefined()
  })

  it("approves only a call whose decision is an approval that approved", async () => {
    const id = await run({ kind: 'browser' })
    const decide = async (call: string, kind: string, outcome: string) =>
      t.db.sql`
        INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id, call_id, kind, outcome, responder)
        VALUES (${flowRequestId(id, 'wr1', call)}, ${id}, 'wr1', ${call}, ${kind}, ${outcome}, 'browser')`
    await decide('c1', 'approval', 'approved')
    await decide('c2', 'approval', 'denied')
    await decide('c3', 'answer', 'answered')
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c1'))).toBe(true)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c2'))).toBe(false)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c3'))).toBe(false)
    // Another execution's (a Reset's) or a call never decided.
    expect(await flows.approved(flowRequestId(id, 'wr2', 'c1'))).toBe(false)
    expect(await flows.approved(flowRequestId(id, 'wr1', 'c4'))).toBe(false)
  })
})
