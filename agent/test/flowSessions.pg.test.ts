import { randomUUID } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { ApplicationFailure } from '@temporalio/common'
import { MockActivityEnvironment } from '@temporalio/testing'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { EventLog } from '../src/sessions/eventLog.js'
import { event } from '../src/sessions/protocol.js'
import { PgFlowRuns } from '../src/temporal/flowRuns.js'
import {
  FLOW_SESSION_CHECK,
  FLOW_SESSION_TURN,
  flowSessionActivities,
  SESSION_REFUSED,
  TURN_FAILED,
  type TurnAnswer,
} from '../src/temporal/flowSessions.js'
import { NOT_FOR_FLOWS } from '../src/temporal/toolActivities.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// A flow's `agent(...)` and `ask_session(...)` (#1057, phase 6g): the activities on
// `agent-tools` that check a flow may talk to a session and wait for its turn's answer.

const MIGRATIONS = fileURLToPath(new URL('./fixtures/backend-flow-migrations/', import.meta.url))

describe.skipIf(!TEST_DATABASE_URL)(`flow session activities${TEST_DATABASE_URL ? '' : ` (set ${TEST_DATABASE_URL_ENV})`}`, () => {
  let t: Throwaway
  let log: EventLog
  let acts: ReturnType<typeof flowSessionActivities>
  let runId: string

  beforeEach(async () => {
    t = await throwawayDatabase()
    expect(await t.db.ready()).toBe(true)
    for (const file of readdirSync(MIGRATIONS).sort()) await t.db.sql.unsafe(readFileSync(`${MIGRATIONS}${file}`, 'utf8'))
    await t.db.sql`
      INSERT INTO workflow_definitions (id, name, version, script, created_by)
      VALUES ('d1', 'swap', 1, 'x', '{"kind": "browser"}')`
    runId = randomUUID()
    await t.db.sql`
      INSERT INTO workflow_runs (id, definition_id, version, name, status, workflow_id, workflow_run_id, started_by)
      VALUES (${runId}, 'd1', 1, 'swap', 'running', ${`flow-${runId}`}, 'wr1', '{"kind": "browser"}')`
    log = new EventLog(t.db.sql)
    acts = flowSessionActivities({ sql: t.db.sql, flows: new PgFlowRuns(t.db.sql), pollMs: 20 })
  })
  afterEach(async () => {
    await t?.drop()
  })

  async function session(opts: { mode?: string; owner?: [string, string] } = {}): Promise<string> {
    const id = randomUUID()
    const [kind, owner] = opts.owner ?? ['browser', 'browser']
    await t.db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
      VALUES (${id}, 'mcp', ${kind}, ${owner}, 'Someone', ${kind}, ${owner}, 'running', 10, 1, ${opts.mode ?? 'durable'})`
    return id
  }

  function env(workflowId = `flow-${runId}`) {
    return new MockActivityEnvironment({
      activityId: 'turn-agent-c1',
      workflowExecution: { workflowId, runId: 'wr1' },
      activityType: FLOW_SESSION_TURN,
      taskQueue: 'agent-tools',
    })
  }

  const turn = async (sid: string, turnId: string, workflowId?: string): Promise<TurnAnswer> => {
    const answer: unknown = await env(workflowId).run(acts[FLOW_SESSION_TURN]!, { session_id: sid, turn_id: turnId })
    return answer as TurnAnswer
  }
  const check = (sid: string, workflowId?: string) => env(workflowId).run(acts[FLOW_SESSION_CHECK]!, { session_id: sid })

  async function failure(p: Promise<unknown>): Promise<ApplicationFailure> {
    const err = await p.then(
      () => undefined,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApplicationFailure)
    return err as ApplicationFailure
  }

  const say = (sid: string, messageId: string, text: string) => [
    event({ type: 'assistant.text.delta', sessionId: sid, messageId, delta: text.slice(0, 3) }),
    event({ type: 'assistant.text.delta', sessionId: sid, messageId, delta: text.slice(3) }),
    event({ type: 'assistant.text.done', sessionId: sid, messageId }),
  ]

  it("waits for the turn's end and answers with its last message and its outward calls", async () => {
    const sid = await session()
    const owner = { kind: 'browser' as const, id: 'browser', label: 'You' }
    // An earlier turn's text is not this turn's.
    await log.append(sid, [event({ type: 'user.turn', sessionId: sid, turnId: 'old', text: 'hi', author: owner }), ...say(sid, 'm0', 'earlier')])
    const waiting = turn(sid, 't1')
    await log.append(sid, [
      event({ type: 'user.turn', sessionId: sid, turnId: 't1', text: 'print it', author: owner }),
      ...say(sid, 'm1', 'thinking'),
      event({ type: 'tool.call', sessionId: sid, id: 'a', name: 'render_model', input: {}, risk: 'write' }),
      event({ type: 'tool.call', sessionId: sid, id: 'b', name: 'print_output', input: {}, risk: 'outward' }),
      event({ type: 'tool.result', sessionId: sid, id: 'b', ok: true, summary: 'queued' }),
    ])
    await new Promise((r) => setTimeout(r, 60))
    await log.append(sid, [
      ...say(sid, 'm2', '{"done": true}'),
      event({ type: 'session.result', sessionId: sid, turns: 1 }),
      event({ type: 'session.status', sessionId: sid, status: 'idle' }),
    ])
    expect(await waiting).toEqual({ session_id: sid, turn_id: 't1', text: '{"done": true}', outward_calls: 1 })
  })

  it('fails a turn that ended in an error, never to be retried', async () => {
    const sid = await session()
    const owner = { kind: 'browser' as const, id: 'browser', label: 'You' }
    await log.append(sid, [
      event({ type: 'user.turn', sessionId: sid, turnId: 't1', text: 'go', author: owner }),
      event({ type: 'session.result', sessionId: sid, turns: 1 }),
      event({ type: 'error', sessionId: sid, code: 'interrupted', message: 'the turn was interrupted' }),
      event({ type: 'session.status', sessionId: sid, status: 'idle' }),
    ])
    const err = await failure(turn(sid, 't1'))
    expect([err.type, err.nonRetryable, err.message]).toEqual([TURN_FAILED, true, 'the turn was interrupted'])
  })

  it("lets a flow talk only to a durable session its starter owns", async () => {
    expect(await check(await session())).toEqual({ session_id: expect.any(String), mode: 'durable' })
    for (const sid of [await session({ mode: 'classic' }), await session({ owner: ['bearer', 'token:9'] }), randomUUID()]) {
      const err = await failure(check(sid))
      expect([err.type, err.nonRetryable]).toEqual([SESSION_REFUSED, true])
      const waited = await failure(turn(sid, 't1'))
      expect(waited.type).toBe(SESSION_REFUSED)
    }
  })

  it('serves only flow runs', async () => {
    const sid = await session()
    const err = await failure(check(sid, `session-${sid}`))
    expect([err.type, err.nonRetryable]).toEqual([NOT_FOR_FLOWS, true])
  })
})
