import { randomUUID } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { childReasoning, FlowQueue, REASONING_MAX } from '../src/flows/queue.js'
import { durableRequestId, flowRequestId } from '../src/gate/ids.js'
import { pendingInput } from '../src/routes/pendingInput.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { manager, tempPaths } from './support/sessions.js'

// A flow run's one queue (#2482, spec 2026-10-11 §4.5): its own parked calls, its child
// sessions' and its requested grant, in one read, each with the step, why it is outside
// the grant, its hops, and for a child the text its model wrote before the call. The
// person's aggregate read (the badge's) has the run's own calls too, marked by run.

const MIGRATIONS = fileURLToPath(new URL('./fixtures/backend-flow-migrations/', import.meta.url))
const RUN = '7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b'
const TOKEN = 'token:0f0e0d0c-0b0a-4908-8706-050403020100'

describe.skipIf(!TEST_DATABASE_URL)(`flow run queue${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let queue: FlowQueue
  let parent: string
  let child: string

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    for (const file of readdirSync(MIGRATIONS).sort()) await db.sql.unsafe(readFileSync(`${MIGRATIONS}${file}`, 'utf8'))
    queue = new FlowQueue(db.sql)
    parent = await session('bearer', TOKEN)
    child = await session('flow', `flow:${RUN}`)
  })
  afterEach(async () => {
    await drop()
  })

  async function session(kind: string, id: string): Promise<string> {
    const sid = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
      VALUES (${sid}, 'chat', ${kind}, ${id}, 'Owner', ${kind}, ${id}, 'waiting_approval', 10, 1, 'durable')`
    return sid
  }

  async function run(): Promise<void> {
    await db.sql`
      INSERT INTO workflow_definitions (id, name, version, script, created_by)
      VALUES ('d1', 'swap', 1, 'x', '{"kind": "browser"}') ON CONFLICT DO NOTHING`
    const at = new Date().toISOString()
    const steps = [
      { seq: 1, fn: 'ask_session', call_id: 'c0', status: 'running', outward: false, started_at: at, history_length: 5, session_id: child },
      { seq: 2, fn: 'queue_print', call_id: 'c1', status: 'running', outward: true, started_at: at, history_length: 9 },
    ]
    const waiting = [
      {
        call_id: 'c1',
        kind: 'approval',
        fn: 'queue_print',
        since: at,
        hop: 'person',
        hop_until: null,
        reason: 'print limit reached',
        proposed: true,
        hops: [{ hop: 'parent', action: 'narrow', actor: `session:${parent}`, at }],
      },
    ]
    await db.sql`
      INSERT INTO workflow_runs (id, definition_id, version, name, status, workflow_id, workflow_run_id, started_by, waiting_on, steps)
      VALUES (${RUN}, 'd1', 1, 'swap', 'waiting', ${`flow-${RUN}`}, 'wr1',
              ${db.sql.json({ kind: 'agent', principal: TOKEN, session: parent })}, ${db.sql.json(waiting as never)},
              ${db.sql.json(steps as never)})`
  }

  async function park(hop: 'parent' | 'person' = 'parent', toolUseId = `toolu_${randomUUID()}`): Promise<string> {
    const id = durableRequestId(child, 'run-1', toolUseId)
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
                                    requested_by, responders, expires_at, flow_run_id, hop, hop_until, parent_session,
                                    parent_principal, outside_reason, hops)
      VALUES (${id}, ${child}, ${`session-${child}`}, 'run-1', 'approval', 'print_output', 'print box on printer 5', ${'a'.repeat(64)}, '',
              ${db.sql.json({ kind: 'flow', id: `flow:${RUN}`, label: 'Flow' })}, ${['browser']}, now() + interval '1 hour',
              ${RUN}, ${hop}, ${hop === 'parent' ? db.sql`now() + interval '10 minutes'` : null},
              ${hop === 'parent' ? parent : null}, ${hop === 'parent' ? TOKEN : null},
              'printer 5 is not in the grant', ${db.sql.json([{ hop, action: 'parked', actor: 'flow', at: new Date().toISOString() }])})`
    return id
  }

  let seq = 0
  async function log(sessionId: string, ...events: Record<string, unknown>[]): Promise<void> {
    for (const e of events) {
      seq += 1
      await db.sql`INSERT INTO ai_session_events (session_id, seq, event) VALUES (${sessionId}, ${seq}, ${JSON.stringify({ v: 1, sessionId, ...e })})`
    }
  }
  const text = (messageId: string, delta: string) => ({ type: 'assistant.text.delta', messageId, delta })

  it("lists the run's own calls, its child's and its requested grant, each with its step, reason and hops", async () => {
    await run()
    const toolu = `toolu_${randomUUID()}`
    const childCall = await park('parent', toolu)
    await log(child, { type: 'user.turn', turnId: 't1', text: 'slice it' }, text('m1', 'Printer 5 is free, '), text('m1', 'so I will print there.'), {
      type: 'tool.call',
      id: toolu,
      name: 'print_output',
      input: {},
      risk: 'outward',
    })
    await db.sql`
      INSERT INTO flow_grants (id, run_id, status, spec, requested_by) VALUES ('g1', ${RUN}, 'requested', '{}', '{"kind": "agent"}')`

    const q = await queue.forRun(RUN)
    expect(q).toMatchObject({ run_id: RUN, name: 'swap', status: 'waiting', starter_session: parent })
    expect(q!.entries).toEqual([
      expect.objectContaining({
        source: 'flow',
        id: flowRequestId(RUN, 'wr1', 'c1'),
        fn: 'queue_print',
        hop: 'person',
        reason: 'print limit reached',
        proposed: true,
        step: { seq: 2, fn: 'queue_print' },
        hops: [expect.objectContaining({ action: 'narrow' })],
      }),
      expect.objectContaining({
        source: 'child',
        id: childCall,
        session_id: child,
        tool: 'print_output',
        summary: 'print box on printer 5',
        hop: 'parent',
        reason: 'printer 5 is not in the grant',
        step: { seq: 1, fn: 'ask_session' },
        reasoning: 'Printer 5 is free, so I will print there.',
      }),
      { source: 'grant', id: 'g1', status: 'requested', created_at: expect.any(String) },
    ])
    // Never a call's input, nor a proposal's.
    expect(JSON.stringify(q)).not.toContain('"input"')
    expect(await queue.forRun('no-such-run')).toBeUndefined()
  })

  it("keeps a child's reasoning to its turn before the call, and to its last 2 KiB", async () => {
    const toolu = `toolu_${randomUUID()}`
    const id = durableRequestId(child, 'run-1', toolu)
    await log(
      child,
      { type: 'user.turn', turnId: 't0', text: 'earlier' },
      text('m0', 'An earlier turn.'),
      { type: 'user.turn', turnId: 't1', text: 'now' },
      text('m1', 'First thought.'),
      text('m2', 'x'.repeat(REASONING_MAX)),
      { type: 'tool.call', id: toolu, name: 'print_output', input: {}, risk: 'outward' },
      text('m3', 'After the call.'),
    )
    const reasoning = await childReasoning(db.sql, id)
    expect(reasoning).toHaveLength(REASONING_MAX)
    expect(reasoning).toBe('x'.repeat(REASONING_MAX))
    expect(reasoning).not.toContain('earlier')

    const short = `toolu_${randomUUID()}`
    await log(child, { type: 'user.turn', turnId: 't2', text: 'again' }, text('m4', 'One.'), text('m5', 'Two.'), {
      type: 'tool.call',
      id: short,
      name: 'print_output',
      input: {},
      risk: 'outward',
    })
    expect(await childReasoning(db.sql, durableRequestId(child, 'run-1', short))).toBe('One.\n\nTwo.')
    // Not logged yet: nothing.
    expect(await childReasoning(db.sql, durableRequestId(child, 'run-1', 'toolu_unlogged'))).toBe('')
  })

  it("puts the run's own calls in the person's aggregate read, marked by run, and in no one else's", async () => {
    await run()
    const childCall = await park('parent')
    const m = manager({ sql: db.sql, paths: await tempPaths() })
    const page = await pendingInput(m, undefined, { runs: true })
    expect(page.entries).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: flowRequestId(RUN, 'wr1', 'c1'),
          kind: 'approval',
          tool: 'queue_print',
          session_id: null,
          responders: ['browser'],
          run: { id: RUN, name: 'swap', hop: 'person', reason: 'print limit reached', starter_session: parent },
        }),
        expect.objectContaining({ id: childCall, run: { id: RUN, hop: 'parent', reason: 'printer 5 is not in the grant' } }),
      ]),
    )
    // The tools' read (`pending_input_list`) and another principal's never list them.
    expect((await pendingInput(m)).entries.map((e) => e.id)).not.toContain(flowRequestId(RUN, 'wr1', 'c1'))
    const other = { kind: 'bearer' as const, id: TOKEN, label: 'Agent' }
    expect((await pendingInput(m, other, { runs: true })).entries.map((e) => e.id)).not.toContain(flowRequestId(RUN, 'wr1', 'c1'))
  })
})
