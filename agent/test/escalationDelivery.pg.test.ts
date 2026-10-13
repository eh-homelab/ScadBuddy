import { randomUUID } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Tier } from '../src/auth/principal.js'
import type { Database } from '../src/db.js'
import { Escalations, type EscalationSend } from '../src/flows/escalations.js'
import { durableRequestId, flowRequestId } from '../src/gate/ids.js'
import { SessionError } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// Delivery of a flow run's calls waiting at its starter session (#2482, spec 2026-10-11
// §4.4): one turn per idle starter session, listing what waits for it, the details in
// the untrusted-data envelope, sent with the tiers its owner holds now. A session that
// cannot be asked (gone, archived, handed off, a token revoked, out of budget) is
// skipped: a child's entries go to the person at once.

const MIGRATIONS = fileURLToPath(new URL('./fixtures/backend-flow-migrations/', import.meta.url))
const RUN = '7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b'
const TOKEN_ID = '0f0e0d0c-0b0a-4908-8706-050403020100'
const TOKEN = `token:${TOKEN_ID}`

type Sent = { sessionId: string; owner: Owner; text: string; tiers: readonly Tier[] }

describe.skipIf(!TEST_DATABASE_URL)(`escalation delivery${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let sent: Sent[]
  let refuse: SessionError | undefined
  let escalations: Escalations
  let parent: string
  let child: string

  const send: EscalationSend = async (sessionId, owner, text, tiers) => {
    if (refuse) throw refuse
    sent.push({ sessionId, owner, text, tiers })
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    for (const file of readdirSync(MIGRATIONS).sort()) await db.sql.unsafe(readFileSync(`${MIGRATIONS}${file}`, 'utf8'))
    sent = []
    refuse = undefined
    escalations = new Escalations({ sql: db.sql, durable: () => undefined, send })
    parent = await session('bearer', TOKEN)
    child = await session('flow', `flow:${RUN}`)
    await db.sql`INSERT INTO ai_mcp_tokens (id, name, token_hash, tier) VALUES (${TOKEN_ID}, 'parent', ${'a'.repeat(64)}, 'outward')`
  })
  afterEach(async () => {
    await drop()
  })

  async function session(kind: string, id: string): Promise<string> {
    const sid = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
      VALUES (${sid}, 'chat', ${kind}, ${id}, 'Owner', ${kind}, ${id}, 'idle', 10, 1, 'durable')`
    return sid
  }

  async function park(parentSession = parent): Promise<string> {
    const id = durableRequestId(child, 'run-1', `toolu_${randomUUID()}`)
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
                                    requested_by, responders, expires_at, flow_run_id, hop, hop_until, parent_session,
                                    parent_principal, outside_reason)
      VALUES (${id}, ${child}, ${`session-${child}`}, 'run-1', 'approval', 'print_output', 'print box on printer 5', ${'a'.repeat(64)}, '',
              ${db.sql.json({ kind: 'flow', id: `flow:${RUN}`, label: 'Flow' })}, ${['browser']}, now() + interval '1 hour',
              ${RUN}, 'parent', now() + interval '10 minutes', ${parentSession}, ${TOKEN}, 'printer 5 is not in the grant')`
    return id
  }

  async function flowRun(parentSession = parent): Promise<string> {
    await db.sql`
      INSERT INTO workflow_definitions (id, name, version, script, created_by)
      VALUES ('d1', 'swap', 1, 'x', '{"kind": "browser"}') ON CONFLICT DO NOTHING`
    const waiting = [
      {
        call_id: 'c1',
        kind: 'approval',
        fn: 'queue_print',
        since: new Date().toISOString(),
        hop: 'parent',
        hop_until: new Date(Date.now() + 600_000).toISOString(),
        reason: 'print limit reached',
        proposed: false,
        hops: [],
      },
    ]
    await db.sql`
      INSERT INTO workflow_runs (id, definition_id, version, name, status, workflow_id, workflow_run_id, started_by, waiting_on)
      VALUES (${RUN}, 'd1', 1, 'swap', 'waiting', ${`flow-${RUN}`}, 'wr1',
              ${db.sql.json({ kind: 'agent', principal: TOKEN, session: parentSession })}, ${db.sql.json(waiting as never)})`
    return flowRequestId(RUN, 'wr1', 'c1')
  }

  async function hop(id: string): Promise<{ hop: string; delivered: boolean } | undefined> {
    const [r] = await db.sql<{ hop: string; delivered: boolean }[]>`
      SELECT hop, delivered_at IS NOT NULL AS delivered FROM ai_pending_input WHERE request_id = ${id}`
    return r
  }

  it("sends an idle starter session one turn with what waits for it, as untrusted data, at its owner's tiers", async () => {
    const childCall = await park()
    const ownCall = await flowRun()
    expect(await escalations.deliver()).toBe(2)
    expect(sent).toHaveLength(1)
    const [turn] = sent
    expect(turn!.sessionId).toBe(parent)
    expect(turn!.owner).toMatchObject({ kind: 'bearer', id: TOKEN })
    expect(turn!.tiers).toEqual(['read', 'write', 'outward'])
    expect(turn!.text).toContain('escalation_respond')
    // The calls' details are tool and model output: inside the envelope, never bare.
    const envelope = turn!.text.slice(turn!.text.indexOf('{'))
    const data = JSON.parse(envelope) as { untrusted_data: { content: { request_id: string; reason: string }[] } }
    expect(data.untrusted_data.content.map((c) => c.request_id).sort()).toEqual([childCall, ownCall].sort())
    expect(turn!.text.slice(0, turn!.text.indexOf('{'))).not.toContain('printer 5')
    expect((await hop(childCall))?.delivered).toBe(true)
    // Once.
    expect(await escalations.deliver()).toBe(0)
    expect(sent).toHaveLength(1)
  })

  it('a busy starter session gets them on a later tick', async () => {
    const id = await park()
    refuse = new SessionError('busy', 'a turn is running')
    expect(await escalations.deliver()).toBe(0)
    expect((await hop(id))?.delivered).toBe(false)
    refuse = undefined
    expect(await escalations.deliver()).toBe(1)
    expect(sent).toHaveLength(1)
  })

  it("a starter session that cannot be asked is skipped: a child's call goes to the person at once", async () => {
    const out = await park()
    refuse = new SessionError('budget_exhausted', 'the session has spent its budget')
    expect(await escalations.deliver()).toBe(0)
    expect(await hop(out)).toMatchObject({ hop: 'person' })

    refuse = undefined
    const archived = await park()
    await db.sql`UPDATE ai_sessions SET archived_at = now() WHERE id = ${parent}`
    expect(await escalations.deliver()).toBe(0)
    expect(await hop(archived)).toMatchObject({ hop: 'person' })
    expect(sent).toEqual([])
  })

  it('a handed-off starter session, or a revoked token, is no parent', async () => {
    const handed = await park()
    await db.sql`UPDATE ai_sessions SET owner_kind = 'browser', owner_id = 'browser' WHERE id = ${parent}`
    expect(await escalations.deliver()).toBe(0)
    expect(await hop(handed)).toMatchObject({ hop: 'person' })

    await db.sql`UPDATE ai_sessions SET owner_kind = 'bearer', owner_id = ${TOKEN} WHERE id = ${parent}`
    await db.sql`UPDATE ai_mcp_tokens SET revoked_at = now() WHERE id = ${TOKEN_ID}`
    const revoked = await park()
    expect(await escalations.deliver()).toBe(0)
    expect(await hop(revoked)).toMatchObject({ hop: 'person' })
    expect(sent).toEqual([])
  })
})
