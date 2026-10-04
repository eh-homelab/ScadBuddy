import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { attentionCard, type AttentionSpec, parseAttention } from '../src/harness/attention.js'
import { ATTENTION_TOOL, type QuestionVerdict } from '../src/harness/questions.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { ATTENTION_RATE_LIMIT } from '../src/questions/service.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PROTOCOL_VERSION, type ServerEvent } from '../src/sessions/protocol.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'

// Attention requests (#815) at the question gate, in Postgres: parked as an
// `ai_questions` row of kind 'attention', answered like a question, and timed
// out by #815's rule, which never answers. The SDK side is
// test/attention.sdk.test.ts.

const input = (over: Record<string, unknown> = {}) => {
  const parsed = parseAttention({ reason: 'tab_disconnected', message: 'The ScadBuddy tab closed; reopen it?', ...over })
  if (!parsed.ok) throw new Error(parsed.error)
  return parsed.input
}

const answer = (sessionId: string, id: string, answers: string[]) =>
  ({ v: PROTOCOL_VERSION, type: 'question.answer', sessionId, id, answers }) as const

describe.skipIf(!TEST_DATABASE_URL)(`attention requests in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let verdicts: QuestionVerdict[]

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    verdicts = []
  })
  afterEach(async () => {
    await drop()
  })

  /** A turn that raises each request in `requests` through its gate, one after another, then ends. */
  const raising = (...requests: { spec: AttentionSpec; message?: string }[]) => (run: HarnessRun): AsyncIterable<SDKMessage> =>
    (async function* () {
      await Promise.resolve()
      for (const [i, r] of requests.entries()) {
        const card = attentionCard(input({ message: r.message ?? 'The ScadBuddy tab closed; reopen it?' }))
        verdicts.push(
          await run.questionGate!({
            tool: ATTENTION_TOOL,
            questions: [card],
            toolUseId: `toolu_a${i}`,
            signal: new AbortController().signal,
            attention: r.spec,
          }),
        )
      }
      // As the SDK does: an aborted query ends without a result.
      if (run.signal?.aborted) return
      yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
    })()

  const spec = (over: Partial<AttentionSpec> = {}): AttentionSpec => ({ reason: 'tab_disconnected', onTimeout: 'proceed', timeoutS: 300, ...over })

  async function events(m: SessionManager, sessionId: string): Promise<ServerEvent[]> {
    return (await m.events.read(sessionId)).map((e) => e.event)
  }

  async function pending(sessionId: string): Promise<string> {
    let id: string | undefined
    await expect.poll(async () => {
      const [row] = await db.sql<{ id: string }[]>`SELECT id FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL`
      id = row?.id
      return id
    }).toBeDefined()
    return id!
  }

  it('parks the turn in waiting_input as an attention card, and the user acknowledges it with a reply', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec() }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'render it in my tab' })
    const id = await pending(session.id)
    await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_input')

    const [row] = await db.sql`
      SELECT kind, attention_reason, on_timeout, tool, extract(epoch FROM expires_at - created_at)::int AS window
      FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ kind: 'attention', attention_reason: 'tab_disconnected', on_timeout: 'proceed', tool: ATTENTION_TOOL, window: 300 })

    await m.questions.answer(browser, answer(session.id, id, ["I'm here"]))
    await turn!.done
    expect(verdicts).toEqual([{ answered: true, answers: { 'The ScadBuddy tab closed; reopen it?': "I'm here" } }])

    const log = await events(m, session.id)
    const asked = log.find((e) => e.type === 'question.asked')
    expect(asked).toMatchObject({
      id,
      tool: 'toolu_a0',
      questions: [{ question: 'The ScadBuddy tab closed; reopen it?', header: 'Tab disconnected', options: [{ label: "I'm here" }, { label: 'Carry on without me' }] }],
      attention: { reason: 'tab_disconnected', onTimeout: 'proceed', expiresAt: expect.any(String) },
    })
    await expectPanelAccepts(log)
  })

  it("is listed as pending for the badge while it waits, with its timer, and not once it is answered", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ reason: 'blocked' }) }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    expect(await m.questions.listPending()).toEqual([
      expect.objectContaining({ id, sessionId: session.id, kind: 'attention', attentionReason: 'blocked', onTimeout: 'proceed', expiresAt: expect.any(String) }),
    ])
    await m.questions.answer(browser, answer(session.id, id, ['Carry on without me']))
    await turn!.done
    expect(await m.questions.listPending()).toEqual([])
  })

  // #815 §4: the timer never answers for the user, and never approves.
  it("on_timeout 'proceed': the timer resolves it as timed_out, never answered, and the turn goes on", async () => {
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ timeoutS: 0.3 }) }), approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    await turn!.done

    expect(verdicts).toEqual([{ answered: false, timedOut: true, message: expect.stringMatching(/nobody replied/) }])
    const [row] = await db.sql`SELECT outcome, answers, answered_by_kind FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'timed_out', answers: null, answered_by_kind: null })
    const log = await events(m, session.id)
    const resolved = log.find((e) => e.type === 'question.resolved')
    expect(resolved).toMatchObject({ id, answered: false, reason: expect.stringMatching(/proceed/) })
    expect(resolved).not.toHaveProperty('answers')
    expect(resolved).not.toHaveProperty('by')
    // The turn finished on its own: not interrupted.
    expect(log.filter((e) => e.type === 'error')).toEqual([])
    expect((await m.get(session.id, browser)).status).toBe('idle')
    await expectPanelAccepts(log)
    expect(await db.sql`SELECT action, surface, principal_kind, outcome FROM ai_audit WHERE kind = 'question'`).toEqual([
      { action: 'timed_out', surface: 'system', principal_kind: 'system', outcome: 'refused' },
    ])
  })

  it("on_timeout 'stop': the timer ends the turn as an interrupt does", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ onTimeout: 'stop', timeoutS: 0.3 }) }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    await turn!.done

    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not reply.*on_timeout: stop.*turn ends/) }])
    const [row] = await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'timed_out' })
    const log = await events(m, session.id)
    expect(log.find((e) => e.type === 'question.resolved')).toMatchObject({ id, answered: false, reason: expect.stringMatching(/on_timeout: stop/) })
    expect(log.find((e) => e.type === 'error')).toMatchObject({ code: 'interrupted' })
    expect((await m.get(session.id, browser)).status).toBe('idle')
  })

  it('an interrupt cancels it like a question: nothing is answered and nothing times out', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec() }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    expect(await m.interrupt(session.id, browser)).toBe(true)
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
    expect((await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`)[0]).toEqual({ outcome: 'cancelled' })
  })

  // #815 §5: one open request per session per reason; a new one replaces the last.
  it('a second request for the same reason supersedes the open one; another reason does not', async () => {
    const supersedes = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const ask = (toolUseId: string, s: AttentionSpec) =>
          run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId, signal: new AbortController().signal, attention: s })
        const first = ask('toolu_1', spec())
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(1)
        const other = ask('toolu_2', spec({ reason: 'blocked', timeoutS: 0.3 }))
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(2)
        const second = ask('toolu_3', spec({ timeoutS: 0.3 }))
        verdicts.push(await first, await other, await second)
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: supersedes, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(verdicts).toEqual([
      { answered: false, message: expect.stringMatching(/replaced by a newer request/) },
      { answered: false, timedOut: true, message: expect.any(String) },
      { answered: false, timedOut: true, message: expect.any(String) },
    ])
    const rows = await db.sql`SELECT tool_use_id, outcome FROM ai_questions WHERE session_id = ${session.id} ORDER BY created_at`
    expect(rows).toEqual([
      { tool_use_id: 'toolu_1', outcome: 'cancelled' },
      { tool_use_id: 'toolu_2', outcome: 'timed_out' },
      { tool_use_id: 'toolu_3', outcome: 'timed_out' },
    ])
  })

  it('refuses past the per-user rate limit without parking', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec() }), approvalPollMs: 20 })
    const { session: other } = await m.start(browser, { origin: 'chat', title: 'earlier' })
    for (let i = 0; i < ATTENTION_RATE_LIMIT; i++) {
      await db.sql`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout,
                                  expires_at, outcome, reason, resolved_at)
        VALUES (gen_random_uuid(), ${other.id}, gen_random_uuid(), ${ATTENTION_TOOL}, ${`toolu_old${i}`}, '[]',
                'attention', 'done', 'proceed', now(), 'timed_out', 'old', now())`
    }
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/at most 10 attention requests/) }])
    expect(await db.sql`SELECT 1 FROM ai_questions WHERE session_id = ${session.id}`).toEqual([])
  })

  it('a question cannot be timed out, by the schema itself', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'q' })
    await expect(db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, outcome, reason, resolved_at)
      VALUES (gen_random_uuid(), ${session.id}, gen_random_uuid(), 'AskUserQuestion', 'toolu_q', '[]', 'timed_out', 'x', now())`).rejects.toThrow(
      /ai_questions_outcome_check/,
    )
  })

  // Spec §6.6's aggregate read, for the badge: every kind in one shape.
  it('GET /api/v1/ai/pending-input lists the parked approval, question and attention request, and only for the UI', async () => {
    const all = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const card = attentionCard(input({ reason: 'blocked', message: 'Load the PETG, please' }))
        const att = run.questionGate!({ tool: ATTENTION_TOOL, questions: [card], toolUseId: 'toolu_a', signal, attention: spec({ reason: 'blocked' }) })
        const approved = run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
        verdicts.push(await att)
        await approved
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: all, approvalPollMs: 20 })
    const app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
    })
    const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_approvals WHERE decision IS NULL`).length).toBe(1)
    const attId = await pending(session.id)
    // A question parked in another of the user's sessions: also counted.
    const asker = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        await run.questionGate!({
          tool: 'AskUserQuestion',
          questions: [{ question: 'Which colour?', header: 'Colour', multiSelect: false, options: [{ label: 'Red', description: '' }, { label: 'Blue', description: '' }] }],
          toolUseId: 'toolu_q',
          signal: new AbortController().signal,
        })
        if (run.signal?.aborted) return
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m2 = manager({ sql: db.sql, paths: await tempPaths(), run: asker, approvalPollMs: 20 })
    const { session: s2, turn: t2 } = await m2.start(browser, { origin: 'chat', prompt: 'ask' })
    const qId = await pending(s2.id)

    expect((await app.request('/api/v1/ai/pending-input')).status).toBe(403)
    const response = await app.request('/api/v1/ai/pending-input', { headers: UI_READ })
    expect(response.status).toBe(200)
    const { entries } = (await response.json()) as { entries: Record<string, unknown>[] }
    expect(entries).toEqual([
      expect.objectContaining({ id: `question:${attId}`, kind: 'answer', session_id: session.id, tool: ATTENTION_TOOL, prompt: 'Load the PETG, please', responders: ['browser'], expires_at: expect.any(String), attention: { reason: 'blocked', on_timeout: 'proceed' } }),
      expect.objectContaining({ kind: 'approval', session_id: session.id, tool: 'mcp__stub__print', prompt: '', input_hash: expect.stringMatching(/^[0-9a-f]{64}$/), responders: ['browser', 'grant'] }),
      expect.objectContaining({ id: `question:${qId}`, kind: 'answer', session_id: s2.id, tool: 'AskUserQuestion', prompt: 'Which colour?', expires_at: null }),
    ])
    expect(entries[1]).not.toHaveProperty('attention')
    // Never the outward call's raw input.
    expect(JSON.stringify(entries)).not.toContain('"job"')

    await m.questions.answer(browser, answer(session.id, attId, ['Done, loaded']))
    const approvalId = (await m.approvals.list(browser, { pending: true }))[0]!.id
    await m.approvals.decide(browser, approvalId, false)
    await turn!.done
    await m2.interrupt(s2.id, browser)
    await t2!.done
    const after = (await (await app.request('/api/v1/ai/pending-input', { headers: UI_READ })).json()) as { entries: unknown[] }
    expect(after.entries).toEqual([])
  })
})
