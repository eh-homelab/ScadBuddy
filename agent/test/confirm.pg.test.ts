import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { ApprovalService } from '../src/approvals/service.js'
import type { Database } from '../src/db.js'
import { EventLog } from '../src/sessions/eventLog.js'
import { BACKEND, connect, firstText, services, testApp, type TestApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentB, browser } from './support/sessions.js'

// spec §8.2's prepare/confirm for external MCP clients on ai_approvals (#251,
// #258): an outward call over /mcp records a sessionless approval requested
// by the caller's principal; the browser user decides it; confirm_action runs
// the prepared call only once it is approved, only for that principal, and
// only once.

const OUTPUT = '0123456789abcdef0123456789abcdef'

let deletes: string[] = []
const server = setupServer(
  http.delete(`${BACKEND}/api/v1/models/:slug`, ({ params }) => {
    deletes.push(String(params.slug))
    return new HttpResponse(null, { status: 204 })
  }),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe.skipIf(!TEST_DATABASE_URL)(`confirm_action on ai_approvals${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let approvals: ApprovalService
  let t: TestApp
  const clients: Client[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    approvals = new ApprovalService({ sql: db.sql, events: new EventLog(db.sql), hashKey: Buffer.alloc(32, 3) })
    t = testApp({ services: services({ approvals }) })
    deletes = []
  })
  afterEach(async () => {
    await Promise.all(clients.splice(0).map((c) => c.close()))
    await drop()
  })

  async function caller(name: string) {
    const { token, record } = await t.tokens.mint({ name, tier: 'outward' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
    clients.push(client)
    return { client, principalId: `token:${record.id}` }
  }

  async function call(client: Client, name: string, args: Record<string, unknown>) {
    const result = await client.callTool({ name, arguments: args })
    return { isError: result.isError === true, body: firstText(result) }
  }

  async function prepareDelete(client: Client) {
    const { body } = await call(client, 'delete_model', { slug: 'keychain' })
    return (body as { pending_action_id: string; status: string; expires_at: string }).pending_action_id
  }

  it('records the prepare as a pending, sessionless approval requested by the caller', async () => {
    const a = await caller('a')
    const { body } = await call(a.client, 'delete_model', { slug: 'keychain' })
    const prepared = body as { status: string; pending_action_id: string; expires_at: string; next: string }
    expect(prepared.status).toBe('pending_approval')
    expect(prepared.next).toContain('Nothing was sent')

    const [row] = await approvals.list(browser, { pending: true })
    expect(row).toMatchObject({
      id: prepared.pending_action_id,
      sessionId: null,
      turnId: null,
      tool: 'delete_model',
      tier: 'outward',
      requestedBy: { kind: 'bearer', id: a.principalId },
      decision: null,
      expiresAt: prepared.expires_at,
    })
    // As the MCP server parsed it, defaults applied: what confirm_action will run.
    expect(row?.inputSummary).toBe('{"slug":"keychain","force":false}')
    expect(deletes).toEqual([])
    expect(await call(a.client, 'list_pending_actions', {})).toMatchObject({
      body: [{ pending_action_id: prepared.pending_action_id, tool: 'delete_model', approval: 'pending' }],
    })
  })

  it('refuses to confirm before a decision, runs once after approval, and never twice', async () => {
    const a = await caller('a')
    const id = await prepareDelete(a.client)

    const early = await call(a.client, 'confirm_action', { pending_action_id: id })
    expect(early.isError).toBe(true)
    expect(early.body).toContain('still waiting for a human approval')
    expect(deletes).toEqual([])

    await approvals.decide(browser, id, true)
    expect(await call(a.client, 'list_pending_actions', {})).toMatchObject({ body: [{ approval: 'approved' }] })
    const confirmed = await call(a.client, 'confirm_action', { pending_action_id: id })
    expect(confirmed).toEqual({ isError: false, body: { deleted: 'keychain' } })
    expect(deletes).toEqual(['keychain'])
    expect((await approvals.get(id, browser)).consumedAt).not.toBeNull()

    const again = await call(a.client, 'confirm_action', { pending_action_id: id })
    expect(again.isError).toBe(true)
    expect(deletes).toEqual(['keychain'])
  })

  it('lets only one of two concurrent confirms run the call', async () => {
    const a = await caller('a')
    const id = await prepareDelete(a.client)
    await approvals.decide(browser, id, true)
    const results = await Promise.all([
      call(a.client, 'confirm_action', { pending_action_id: id }),
      call(a.client, 'confirm_action', { pending_action_id: id }),
    ])
    expect(results.filter((r) => !r.isError)).toHaveLength(1)
    expect(deletes).toEqual(['keychain'])
  })

  it("does not confirm another principal's approval, even an approved one", async () => {
    const a = await caller('a')
    const b = await caller('b')
    const id = await prepareDelete(a.client)
    await approvals.decide(browser, id, true)
    const cross = await call(b.client, 'confirm_action', { pending_action_id: id })
    expect(cross.isError).toBe(true)
    expect(cross.body).toContain('no pending action')
    expect(deletes).toEqual([])
    // Another agent without a grant cannot even see it to decide it.
    await expect(approvals.decide(agentB, id, false)).rejects.toThrow(`no approval ${id}`)
    // The approval is still there for its own caller.
    expect((await call(a.client, 'confirm_action', { pending_action_id: id })).isError).toBe(false)
  })

  it('refuses a denied approval, and forgets it', async () => {
    const a = await caller('a')
    const id = await prepareDelete(a.client)
    await approvals.decide(browser, id, false)
    const denied = await call(a.client, 'confirm_action', { pending_action_id: id })
    expect(denied.isError).toBe(true)
    expect(denied.body).toContain('denied')
    expect(deletes).toEqual([])
    expect((await call(a.client, 'list_pending_actions', {})).body).toEqual([])
  })

  it('refuses an approval whose input hash is not the prepared call', async () => {
    const a = await caller('a')
    const id = await prepareDelete(a.client)
    await approvals.decide(browser, id, true)
    await db.sql`UPDATE ai_approvals SET input_hash = ${'0'.repeat(64)} WHERE id = ${id}`
    const res = await call(a.client, 'confirm_action', { pending_action_id: id })
    expect(res.isError).toBe(true)
    expect(res.body).toContain('different call')
    expect(deletes).toEqual([])
  })

  it('refuses an approval that is no longer usable', async () => {
    const a = await caller('a')
    const id = await prepareDelete(a.client)
    await approvals.decide(browser, id, true)
    await db.sql`UPDATE ai_approvals SET usable_until = now() - interval '1 second' WHERE id = ${id}`
    const res = await call(a.client, 'confirm_action', { pending_action_id: id })
    expect(res.isError).toBe(true)
    expect(res.body).toContain('no longer applies')
    expect(deletes).toEqual([])
  })

  it('records nothing for a call whose arguments are refused', async () => {
    const a = await caller('a')
    // A bad argument fails validation before anything is prepared or recorded.
    const bad = await call(a.client, 'send_to_bambuddy', { output_id: 42 })
    expect(bad.isError).toBe(true)
    expect(await approvals.list(browser, { pending: true })).toEqual([])
    const good = await call(a.client, 'send_to_bambuddy', { output_id: OUTPUT })
    expect(good.body).toMatchObject({ status: 'pending_approval' })
    expect(await approvals.list(browser, { pending: true })).toHaveLength(1)
  })
})
